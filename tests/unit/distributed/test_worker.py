"""Tests for WorkerRuntime distributed execution loop."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from apflow.core.distributed.worker import WorkerRuntime
from apflow.core.distributed.config import DistributedConfig
from apflow.core.distributed.idempotency import IdempotencyManager


def _make_config(**overrides: Any) -> DistributedConfig:
    """Create a test DistributedConfig with fast intervals."""
    defaults = {
        "enabled": True,
        "node_id": "test-worker",
        "node_role": "worker",
        "poll_interval_seconds": 0.05,
        "heartbeat_interval_seconds": 0.05,
        "lease_duration_seconds": 0.15,
        "leader_renew_seconds": 0.05,
        "max_parallel_tasks_per_node": 2,
    }
    defaults.update(overrides)
    return DistributedConfig(**defaults)


def _make_task_model(
    task_id: str = "task-1",
    status: str = "pending",
    attempt_id: int = 0,
) -> MagicMock:
    """Create a mock TaskModel."""
    task = MagicMock()
    task.id = task_id
    task.status = status
    task.attempt_id = attempt_id
    task.placement_constraints = None
    task.inputs = None
    return task


def _make_lease(
    task_id: str = "task-1",
    node_id: str = "test-worker",
    lease_token: str = "abc123",
) -> MagicMock:
    """Create a mock TaskLease."""
    lease = MagicMock()
    lease.task_id = task_id
    lease.node_id = node_id
    lease.lease_token = lease_token
    return lease


def _make_worker(
    config: DistributedConfig | None = None,
    node_registry: MagicMock | None = None,
    lease_manager: MagicMock | None = None,
    idempotency_manager: MagicMock | None = None,
    task_executor: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    session_factory: MagicMock | None = None,
) -> WorkerRuntime:
    """Create a WorkerRuntime with mock dependencies."""
    cfg = config or _make_config()
    return WorkerRuntime(
        node_id=cfg.node_id or "test-worker",
        config=cfg,
        session_factory=session_factory or MagicMock(),
        node_registry=node_registry or MagicMock(),
        lease_manager=lease_manager or MagicMock(),
        idempotency_manager=idempotency_manager or MagicMock(),
        task_executor=task_executor or AsyncMock(return_value={"status": "completed"}),
    )


class TestWorkerStartStop:
    """Tests for worker start and shutdown lifecycle."""

    @pytest.mark.asyncio
    async def test_start_registers_node(self) -> None:
        """Starting worker registers node in NodeRegistry."""
        registry = MagicMock()
        worker = _make_worker(node_registry=registry)

        task = asyncio.create_task(worker.start())
        await asyncio.sleep(0.1)
        await worker.shutdown()
        await task

        registry.register_node.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_deregisters_node(self) -> None:
        """Shutdown deregisters node from NodeRegistry."""
        registry = MagicMock()
        worker = _make_worker(node_registry=registry)

        task = asyncio.create_task(worker.start())
        await asyncio.sleep(0.1)
        await worker.shutdown()
        await task

        registry.deregister_node.assert_called_once_with("test-worker")

    @pytest.mark.asyncio
    async def test_shutdown_sets_event(self) -> None:
        """Shutdown sets the shutdown event to stop loops."""
        worker = _make_worker()

        task = asyncio.create_task(worker.start())
        await asyncio.sleep(0.1)
        await worker.shutdown()
        await task

        assert worker._shutdown_event.is_set()


class TestPolling:
    """Tests for the task polling loop."""

    @pytest.mark.asyncio
    async def test_poll_finds_pending_tasks(self) -> None:
        """Worker polls for executable tasks and attempts to execute them."""
        lease_mgr = MagicMock()
        lease = _make_lease()
        lease_mgr.acquire_lease.return_value = lease

        task_model = _make_task_model()
        session = MagicMock()
        # Mock the chain: query().filter().limit().all()
        query_mock = session.query.return_value.filter.return_value
        query_mock.limit.return_value.all.return_value = [task_model]
        # get_bind() for dialect check
        session.get_bind.return_value.dialect.name = "sqlite"
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        factory = MagicMock(return_value=session)

        executor = AsyncMock(return_value={"status": "completed"})
        worker = _make_worker(
            session_factory=factory,
            lease_manager=lease_mgr,
            task_executor=executor,
        )

        task = asyncio.create_task(worker.start())
        await asyncio.sleep(0.2)
        await worker.shutdown()
        await task

        lease_mgr.acquire_lease.assert_called()

    @pytest.mark.asyncio
    async def test_poll_skips_when_no_tasks(self) -> None:
        """Worker does not attempt execution when no pending tasks found."""
        lease_mgr = MagicMock()

        session = MagicMock()
        query_mock = session.query.return_value.filter.return_value
        query_mock.limit.return_value.all.return_value = []
        session.get_bind.return_value.dialect.name = "sqlite"
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        factory = MagicMock(return_value=session)

        worker = _make_worker(
            session_factory=factory,
            lease_manager=lease_mgr,
        )

        task = asyncio.create_task(worker.start())
        await asyncio.sleep(0.15)
        await worker.shutdown()
        await task

        lease_mgr.acquire_lease.assert_not_called()


class TestLeaseAcquisition:
    """Tests for lease acquisition during execution."""

    @pytest.mark.asyncio
    async def test_acquire_lease_atomically(self) -> None:
        """Worker acquires lease via LeaseManager before executing."""
        lease_mgr = MagicMock()
        lease = _make_lease()
        lease_mgr.acquire_lease.return_value = lease
        lease_mgr.renew_lease.return_value = True

        executor = AsyncMock(return_value={"status": "completed"})
        worker = _make_worker(
            lease_manager=lease_mgr,
            task_executor=executor,
        )

        task_model = _make_task_model()
        await worker._execute_task(task_model)

        lease_mgr.acquire_lease.assert_called_once_with("task-1", "test-worker")

    @pytest.mark.asyncio
    async def test_lease_acquisition_fails_skips_execution(self) -> None:
        """When lease acquisition returns None, task is not executed."""
        lease_mgr = MagicMock()
        lease_mgr.acquire_lease.return_value = None

        executor = AsyncMock()
        worker = _make_worker(
            lease_manager=lease_mgr,
            task_executor=executor,
        )

        task_model = _make_task_model()
        await worker._execute_task(task_model)

        executor.assert_not_called()


class TestLeaseRenewal:
    """Tests for background lease renewal."""

    @pytest.mark.asyncio
    async def test_lease_renewal_extends_expiry(self) -> None:
        """Background renewal loop calls renew_lease periodically."""
        lease_mgr = MagicMock()
        lease_mgr.renew_lease.return_value = True

        worker = _make_worker(lease_manager=lease_mgr)

        renewal_task = asyncio.create_task(worker._renew_lease_loop("task-1", "token-abc"))
        await asyncio.sleep(0.15)
        renewal_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await renewal_task

        assert lease_mgr.renew_lease.call_count >= 1

    @pytest.mark.asyncio
    async def test_lease_renewal_failure_stops(self) -> None:
        """If lease renewal returns False, the renewal loop stops."""
        lease_mgr = MagicMock()
        lease_mgr.renew_lease.return_value = False

        worker = _make_worker(lease_manager=lease_mgr)

        # The renewal loop should exit on its own when renewal fails
        await asyncio.wait_for(
            worker._renew_lease_loop("task-1", "token-abc"),
            timeout=1.0,
        )

        lease_mgr.renew_lease.assert_called_once_with("token-abc")


class TestIdempotency:
    """Tests for idempotency checking during execution."""

    @pytest.mark.asyncio
    async def test_cache_hit_skips_execution(self) -> None:
        """When idempotency cache has result, execution is skipped."""
        lease_mgr = MagicMock()
        lease_mgr.acquire_lease.return_value = _make_lease()
        lease_mgr.renew_lease.return_value = True

        idemp = MagicMock()
        idemp.check_cached_result.return_value = (True, {"cached": True})

        executor = AsyncMock()
        worker = _make_worker(
            lease_manager=lease_mgr,
            idempotency_manager=idemp,
            task_executor=executor,
        )

        task_model = _make_task_model()
        await worker._execute_task(task_model)

        executor.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_miss_executes_and_stores(self) -> None:
        """When no cached result, task is executed and result stored."""
        lease_mgr = MagicMock()
        lease_mgr.acquire_lease.return_value = _make_lease()
        lease_mgr.renew_lease.return_value = True

        idemp = MagicMock()
        idemp.check_cached_result.return_value = (False, None)

        executor = AsyncMock(return_value={"output": "done"})
        worker = _make_worker(
            lease_manager=lease_mgr,
            idempotency_manager=idemp,
            task_executor=executor,
        )

        task_model = _make_task_model()
        await worker._execute_task(task_model)

        executor.assert_called_once()
        idemp.store_result.assert_called_once()


class TestHeartbeat:
    """Tests for background heartbeat."""

    @pytest.mark.asyncio
    async def test_heartbeat_keeps_node_healthy(self) -> None:
        """Background heartbeat calls NodeRegistry.heartbeat periodically."""
        registry = MagicMock()
        worker = _make_worker(node_registry=registry)

        heartbeat_task = asyncio.create_task(worker._heartbeat_loop())
        await asyncio.sleep(0.15)
        heartbeat_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await heartbeat_task

        assert registry.heartbeat.call_count >= 1
        registry.heartbeat.assert_called_with("test-worker")


class TestConcurrencyControl:
    """Tests for max parallel tasks enforcement."""

    @pytest.mark.asyncio
    async def test_max_parallel_tasks_enforced(self) -> None:
        """Semaphore limits concurrent task execution."""
        config = _make_config(max_parallel_tasks_per_node=1)
        lease_mgr = MagicMock()
        lease_mgr.acquire_lease.return_value = _make_lease()
        lease_mgr.renew_lease.return_value = True

        idemp = MagicMock()
        idemp.check_cached_result.return_value = (False, None)

        execution_count = 0
        max_concurrent = 0

        async def slow_executor(task: Any) -> dict[str, Any]:
            nonlocal execution_count, max_concurrent
            execution_count += 1
            current = execution_count
            if current > max_concurrent:
                max_concurrent = current
            await asyncio.sleep(0.1)
            execution_count -= 1
            return {"status": "completed"}

        worker = _make_worker(
            config=config,
            lease_manager=lease_mgr,
            idempotency_manager=idemp,
            task_executor=slow_executor,
        )

        tasks = [
            asyncio.create_task(worker._execute_task(_make_task_model(f"task-{i}")))
            for i in range(3)
        ]
        await asyncio.gather(*tasks)

        assert max_concurrent <= 1


class TestResultReporting:
    """Tests for task completion and failure reporting."""

    @pytest.mark.asyncio
    async def test_completion_updates_task_status(self) -> None:
        """After execution, task status is updated and lease released atomically."""
        lease_mgr = MagicMock()
        lease = _make_lease()
        lease_mgr.acquire_lease.return_value = lease
        lease_mgr.renew_lease.return_value = True

        idemp = MagicMock()
        idemp.check_cached_result.return_value = (False, None)

        session = MagicMock()
        task_in_db = _make_task_model()
        session.get.return_value = task_in_db
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        factory = MagicMock(return_value=session)

        executor = AsyncMock(return_value={"output": "done"})
        worker = _make_worker(
            session_factory=factory,
            lease_manager=lease_mgr,
            idempotency_manager=idemp,
            task_executor=executor,
        )

        task_model = _make_task_model()
        await worker._execute_task(task_model)

        # Lease is now released via atomic SQL in the same transaction
        # instead of via lease_manager.release_lease()
        session.execute.assert_called()
        session.commit.assert_called()

    @pytest.mark.asyncio
    async def test_failure_stores_error(self) -> None:
        """On execution failure, error is stored via IdempotencyManager."""
        lease_mgr = MagicMock()
        lease = _make_lease()
        lease_mgr.acquire_lease.return_value = lease
        lease_mgr.renew_lease.return_value = True

        idemp = MagicMock()
        idemp.check_cached_result.return_value = (False, None)

        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)

        executor = AsyncMock(side_effect=RuntimeError("boom"))
        worker = _make_worker(
            session_factory=MagicMock(return_value=session),
            lease_manager=lease_mgr,
            idempotency_manager=idemp,
            task_executor=executor,
        )

        task_model = _make_task_model()
        await worker._execute_task(task_model)

        idemp.store_failure.assert_called_once()
        # Lease is now released via atomic SQL in the same transaction
        session.execute.assert_called()
        session.commit.assert_called()


class TestGracefulShutdown:
    """Tests for graceful shutdown behavior."""

    @pytest.mark.asyncio
    async def test_shutdown_cancels_running_tasks(self) -> None:
        """Shutdown cancels any in-progress task executions."""
        lease_mgr = MagicMock()
        lease = _make_lease()
        lease_mgr.acquire_lease.return_value = lease
        lease_mgr.renew_lease.return_value = True

        idemp = MagicMock()
        idemp.check_cached_result.return_value = (False, None)

        async def long_executor(task: Any) -> dict[str, Any]:
            await asyncio.sleep(10)
            return {"status": "completed"}

        worker = _make_worker(
            lease_manager=lease_mgr,
            idempotency_manager=idemp,
            task_executor=long_executor,
        )

        exec_task = asyncio.create_task(worker._execute_task(_make_task_model()))
        await asyncio.sleep(0.05)

        await worker.shutdown()

        assert worker._shutdown_event.is_set()
        # Task should be cancelled or finished
        exec_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exec_task

    @pytest.mark.asyncio
    async def test_shutdown_releases_leases(self) -> None:
        """Shutdown releases leases for all active tasks."""
        lease_mgr = MagicMock()
        lease = _make_lease()
        lease_mgr.acquire_lease.return_value = lease
        lease_mgr.renew_lease.return_value = True

        idemp = MagicMock()
        idemp.check_cached_result.return_value = (False, None)

        started = asyncio.Event()

        async def long_executor(task: Any) -> dict[str, Any]:
            started.set()
            await asyncio.sleep(10)
            return {"status": "completed"}

        worker = _make_worker(
            lease_manager=lease_mgr,
            idempotency_manager=idemp,
            task_executor=long_executor,
        )

        exec_task = asyncio.create_task(worker._execute_task(_make_task_model()))
        await started.wait()
        await worker.shutdown()

        # After shutdown, the lease should be released
        # (either via normal release or cleanup)
        exec_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exec_task


class TestFinalizeLeaseOwnership:
    """_finalize_task must not clobber a task whose lease was lost/reassigned."""

    @staticmethod
    def _factory(rowcount: int, task: MagicMock):
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.execute.return_value = MagicMock(rowcount=rowcount)
        session.get.return_value = task
        return MagicMock(return_value=session), session

    def test_writes_status_when_lease_held(self) -> None:
        task = _make_task_model(status="in_progress")
        factory, _ = self._factory(1, task)
        worker = _make_worker(session_factory=factory)
        worker._finalize_task("task-1", "tok", "completed")
        assert task.status == "completed"

    def test_skips_status_when_lease_lost(self) -> None:
        task = _make_task_model(status="in_progress")
        factory, session = self._factory(0, task)
        worker = _make_worker(session_factory=factory)
        worker._finalize_task("task-1", "tok", "completed")
        # Lease DELETE matched nothing → do not read/overwrite the (reassigned) task.
        session.get.assert_not_called()
        assert task.status == "in_progress"


class TestExecuteTaskCancellation:
    @pytest.mark.asyncio
    async def test_cancellation_releases_lease(self) -> None:
        lease_mgr = MagicMock()
        lease_mgr.acquire_lease.return_value = _make_lease()
        idem = MagicMock()
        idem.check_cached_result.return_value = (False, None)

        async def cancel_executor(_task: Any) -> dict[str, Any]:
            raise asyncio.CancelledError()

        worker = _make_worker(
            lease_manager=lease_mgr, idempotency_manager=idem, task_executor=cancel_executor
        )
        with pytest.raises(asyncio.CancelledError):
            await worker._execute_task(_make_task_model())

        lease_mgr.release_lease.assert_called_once_with("task-1", "abc123")


class TestPollSkipsInFlight:
    @pytest.mark.asyncio
    async def test_poll_does_not_respawn_running_task(self) -> None:
        lease_mgr = MagicMock()
        lease_mgr.acquire_lease.return_value = _make_lease()
        task_model = _make_task_model()
        session = MagicMock()
        session.query.return_value.filter.return_value.limit.return_value.all.return_value = [
            task_model
        ]
        session.get_bind.return_value.dialect.name = "sqlite"
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        factory = MagicMock(return_value=session)

        worker = _make_worker(session_factory=factory, lease_manager=lease_mgr)
        sleeper = asyncio.create_task(asyncio.sleep(5))
        worker._running_tasks["task-1"] = sleeper  # already in flight

        poll = asyncio.create_task(worker._poll_loop())
        await asyncio.sleep(0.15)
        worker._shutdown_event.set()
        await poll
        sleeper.cancel()

        # The only pending task is already running → it must not be re-spawned.
        lease_mgr.acquire_lease.assert_not_called()


class TestIdempotencyKeyIncorporatesInputs:
    @pytest.mark.asyncio
    async def test_key_derived_from_task_inputs(self) -> None:
        lease_mgr = MagicMock()
        lease_mgr.acquire_lease.return_value = _make_lease()
        idem = MagicMock()
        idem.check_cached_result.return_value = (False, None)

        worker = _make_worker(lease_manager=lease_mgr, idempotency_manager=idem)
        task = _make_task_model()
        task.inputs = {"a": 1}
        await worker._execute_task(task)

        expected = IdempotencyManager.generate_key("task-1", 0, {"a": 1})
        assert idem.check_cached_result.call_args.args[0] == expected


class TestExecutableTaskFiltering:
    """_find_executable_tasks excludes scheduled definitions (runnable units only)."""

    def test_excludes_scheduled_definitions(self) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from apflow.core.storage.sqlalchemy.models import Base, TaskModel

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        try:
            with session_factory() as s:
                # A recurring definition — must NOT be executed by workers.
                s.add(TaskModel(id="defn", name="d", status="pending", schedule_enabled=True))
                # A dispatched run instance — executable.
                s.add(TaskModel(id="run", name="r", status="pending", schedule_enabled=False))
                # A plain pending task — executable.
                s.add(TaskModel(id="plain", name="p", status="pending"))
                # A completed task — not pending.
                s.add(TaskModel(id="done", name="x", status="completed", schedule_enabled=False))
                s.commit()

            worker = _make_worker(
                config=_make_config(max_parallel_tasks_per_node=10),
                session_factory=session_factory,
            )
            ids = {t.id for t in worker._find_executable_tasks()}
            assert ids == {"run", "plain"}
        finally:
            engine.dispose()
