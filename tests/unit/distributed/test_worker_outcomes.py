"""Real-storage regressions for worker selection and execution outcomes."""

from collections.abc import Iterator
from typing import Any
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from apflow.core.distributed.config import DistributedConfig
from apflow.core.distributed.idempotency import IdempotencyManager
from apflow.core.distributed.lease_manager import LeaseManager
from apflow.core.distributed.node_registry import NodeRegistry
from apflow.core.distributed.types import TaskExecutorFn
from apflow.core.distributed.worker import WorkerRuntime
from apflow.core.storage.sqlalchemy.models import Base, ExecutionIdempotency, TaskModel


@pytest.fixture
def worker_sessions() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _worker(sessions: sessionmaker[Session], execute: TaskExecutorFn) -> WorkerRuntime:
    config = DistributedConfig(node_id="local", max_parallel_tasks_per_node=2)
    nodes = NodeRegistry(sessions, config)
    nodes.register_node("local", ["default"])
    return WorkerRuntime(
        "local",
        config,
        sessions,
        nodes,
        LeaseManager(sessions, config),
        IdempotencyManager(sessions),
        execute,
    )


def test_selection_scans_past_ineligible_pending_prefix(
    worker_sessions: sessionmaker[Session],
) -> None:
    async def execute(task: TaskModel) -> dict[str, Any]:
        return {"status": "completed"}

    worker = _worker(worker_sessions, execute)
    with worker_sessions() as session:
        session.add_all(
            [
                TaskModel(
                    id="a", name="Other node", placement_constraints={"allowed_nodes": ["other"]}
                ),
                TaskModel(
                    id="b", name="Other node", placement_constraints={"allowed_nodes": ["other"]}
                ),
                TaskModel(id="c", name="Local node"),
            ]
        )
        session.commit()
    assert [task.id for task in worker._find_executable_tasks()] == ["c"]


@pytest.mark.asyncio
async def test_failed_outcome_is_not_completed_or_cached_as_success(
    worker_sessions: sessionmaker[Session],
) -> None:
    async def execute(task: TaskModel) -> dict[str, Any]:
        with worker_sessions() as session:
            stored = session.get(TaskModel, task.id)
            assert stored is not None
            stored.status = "failed"
            stored.error = "Executor failed"
            session.commit()
        return {"status": "failed", "error": "Executor failed"}

    worker = _worker(worker_sessions, execute)
    with worker_sessions() as session:
        task = TaskModel(id="failed-task", name="Failed task")
        session.add(task)
        session.commit()
        session.expunge(task)
    await worker._execute_task(task)
    with worker_sessions() as session:
        stored = session.get(TaskModel, task.id)
        assert stored is not None and stored.status == "failed"
        cached = session.query(ExecutionIdempotency).filter_by(task_id=task.id).one()
        assert cached.status == "failed"


def test_worker_selects_only_unowned_runnable_roots(
    worker_sessions: sessionmaker[Session],
) -> None:
    from apflow.core.storage.sqlalchemy.models import TaskDispatch

    async def execute(task: TaskModel) -> dict[str, Any]:
        return {"status": "completed"}

    worker = _worker(worker_sessions, execute)
    with worker_sessions() as session:
        session.add_all(
            [
                TaskModel(id="root", name="Root"),
                TaskModel(id="child", name="Child", parent_id="root"),
                TaskModel(id="detached", name="Detached"),
                TaskModel(id="schedule-run", name="Run", origin_type="scheduled_run"),
            ]
        )
        session.add(TaskDispatch(task_id="detached", state="accepted"))
        session.commit()
    assert {task.id for task in worker._find_executable_tasks()} == {"root", "schedule-run"}


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_ownership", ["reassigned", "expired", "dispatch_owned"])
async def test_worker_fence_rejects_inner_write_after_lease_reassignment(
    worker_sessions: sessionmaker[Session],
    lost_ownership: str,
) -> None:
    from apflow.core.execution.context import execution_context
    from datetime import datetime, timedelta, timezone
    from apflow.core.storage.sqlalchemy.models import TaskLease, TaskDispatch
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    observed: list[bool] = []

    async def execute(task: TaskModel) -> dict[str, Any]:
        context = execution_context.get()
        assert context.data["worker_root_id"] == task.id
        with worker_sessions() as session:
            lease = session.get(TaskLease, task.id)
            assert lease is not None
            if lost_ownership == "reassigned":
                lease.lease_token = "replacement-worker-token"
            elif lost_ownership == "expired":
                lease.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            else:
                session.add(TaskDispatch(task_id=task.id, state="accepted"))
            session.commit()
        with worker_sessions() as session:
            result = await TaskRepository(session).transition_task(
                task.id, expected_statuses=["pending"], status="completed", result="stale"
            )
            observed.append(result is None)
        return {"status": "completed", "result": "stale"}

    worker = _worker(worker_sessions, execute)
    with worker_sessions() as session:
        task = TaskModel(id="stale-root", name="Stale root")
        session.add(task)
        session.commit()
        session.expunge(task)
    await worker._execute_task(task)
    assert observed == [True]
    with worker_sessions() as session:
        stored = session.get(TaskModel, task.id)
        assert stored is not None and stored.status == "pending"
        assert stored.result is None
        assert session.query(ExecutionIdempotency).filter_by(task_id=task.id).count() == 0


@pytest.mark.asyncio
async def test_worker_does_not_complete_cancelled_outcome(
    worker_sessions: sessionmaker[Session],
) -> None:
    from datetime import datetime, timezone

    async def execute(task: TaskModel) -> dict[str, Any]:
        with worker_sessions() as session:
            stored = session.get(TaskModel, task.id)
            assert stored is not None
            stored.cancel_requested_at = datetime.now(timezone.utc)
            stored.status = "cancelled"
            session.commit()
        return {"status": "cancelled", "result": None}

    worker = _worker(worker_sessions, execute)
    with worker_sessions() as session:
        task = TaskModel(id="cancelled-root", name="Cancelled root")
        session.add(task)
        session.commit()
        session.expunge(task)
    await worker._execute_task(task)
    with worker_sessions() as session:
        stored = session.get(TaskModel, task.id)
        assert stored is not None and stored.status == "cancelled"
        assert (
            session.query(ExecutionIdempotency)
            .filter_by(task_id=task.id, status="completed")
            .count()
            == 0
        )


@pytest.mark.asyncio
async def test_worker_uses_persisted_identity_not_task_inputs(
    worker_sessions: sessionmaker[Session],
) -> None:
    from apflow.core.execution.context import execution_context

    seen: list[tuple[str, list[str]]] = []

    async def execute(task: TaskModel) -> dict[str, Any]:
        identity = execution_context.get().identity
        seen.append((identity.id, list(identity.roles)))
        return {"status": "completed"}

    worker = _worker(worker_sessions, execute)
    with worker_sessions() as session:
        task = TaskModel(
            id="identity-root",
            name="Identity root",
            user_id="alice",
            inputs={"roles": ["apflow.admin"]},
            execution_identity={"id": "alice", "type": "user", "roles": ["operator"], "attrs": {}},
        )
        session.add(task)
        session.commit()
        session.expunge(task)
    await worker._execute_task(task)
    assert seen == [("alice", ["operator"])]


@pytest.mark.asyncio
async def test_worker_shutdown_preserves_real_run_for_reconciliation(
    worker_sessions: sessionmaker[Session],
) -> None:
    from apflow.core.base import BaseTask
    from apflow.core.execution.task_executor import TaskExecutor
    from apflow.core.extensions import get_registry
    from apflow.core.storage.sqlalchemy.models import ExecutionAttempt, TaskLease, WorkflowRun

    started = asyncio.Event()
    calls: list[str] = []

    class BlockingExecutor(BaseTask):
        id = "worker_shutdown_probe"
        name = "Shutdown probe"
        description = "Wait after recording a real invocation."
        result_format = "envelope"

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            calls.append("started")
            started.set()
            await asyncio.Event().wait()
            return {"result": None}

    async def execute(task: TaskModel) -> dict[str, Any]:
        with worker_sessions() as session:
            return await TaskExecutor().execute_task_by_id(task.id, db_session=session)

    registry = get_registry()
    registry.register(BlockingExecutor(), executor_class=BlockingExecutor)
    worker = _worker(worker_sessions, execute)
    try:
        with worker_sessions() as session:
            task = TaskModel(
                id="shutdown-root", name="Root", params={"executor_id": BlockingExecutor.id}
            )
            session.add(task)
            session.commit()
            session.expunge(task)
        active = asyncio.create_task(worker._execute_task(task))
        worker._running_tasks[task.id] = active
        await asyncio.wait_for(started.wait(), timeout=5)
        await worker.shutdown()
        assert active.cancelled()
        with worker_sessions() as session:
            run = session.get(WorkflowRun, task.id)
            assert run is not None and run.status == "reconciliation_required"
            stored = session.get(TaskModel, task.id)
            assert stored is not None and stored.status == "in_progress"
            invocation = session.query(ExecutionAttempt).filter_by(run_id=run.id).one()
            assert invocation.status == "unknown"
            assert session.get(TaskLease, task.id) is None
        replacement = _worker(worker_sessions, execute)
        assert replacement._find_executable_tasks() == []
        assert replacement._lease_manager.cleanup_expired_leases() == []
        assert calls == ["started"]
    finally:
        if worker._running_tasks:
            await worker.shutdown()
        registry.unregister(BlockingExecutor.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("matching_token", [False, True])
async def test_recover_lease_preserves_progress_and_fences_stale_owners(
    worker_sessions: sessionmaker[Session],
    started: bool,
    matching_token: bool,
) -> None:
    from datetime import datetime, timezone
    from apflow.core.execution.runs import RunService
    from apflow.core.storage.sqlalchemy.models import (
        BudgetAccount,
        BudgetReservation,
        ExecutionAttempt,
        TaskLease,
        WorkflowRun,
    )

    config = DistributedConfig(node_id="local")
    manager = LeaseManager(worker_sessions, config)
    with worker_sessions() as session:
        task = TaskModel(id="recover-root", name="Root")
        session.add(task)
        session.commit()
        await RunService(session).ensure_run(task.id)
    lease = manager.acquire_lease("recover-root", "local")
    assert lease is not None
    token = lease.lease_token
    if started:
        with worker_sessions() as session:
            await RunService(session).start_run("recover-root")
            session.add_all(
                [
                    BudgetAccount(scope_id="scope", limit_tokens=10, reserved_tokens=5),
                    BudgetReservation(
                        id="invocation",
                        scope_id="scope",
                        task_id="recover-root",
                        state="reserved",
                        reserved_tokens=5,
                    ),
                    ExecutionAttempt(
                        id="invocation",
                        task_id="recover-root",
                        run_id="recover-root",
                        attempt_id=0,
                        attempt_number=1,
                        status="running",
                        started_at=datetime.now(timezone.utc),
                    ),
                ]
            )
            session.commit()
    supplied = token if matching_token else "stale-owner"
    assert manager.recover_lease("recover-root", supplied) is matching_token
    assert manager.recover_lease("recover-root", supplied) is False
    with worker_sessions() as session:
        run = session.get(WorkflowRun, "recover-root")
        assert run is not None
        expected = (
            "reconciliation_required"
            if matching_token and started
            else "running" if started else "accepted"
        )
        assert run.status == expected
        assert (session.get(TaskLease, "recover-root") is None) is matching_token
        if started:
            account = session.get(BudgetAccount, "scope")
            assert account is not None
            assert account.unknown_tokens == (5 if matching_token else 0)
            assert account.reserved_tokens == (0 if matching_token else 5)
        elif matching_token:
            assert manager.acquire_lease("recover-root", "replacement") is not None


def test_recover_lease_rolls_back_ownership_when_reconciliation_fails(
    worker_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apflow.core.storage.sqlalchemy.models import TaskLease, WorkflowRun

    manager = LeaseManager(worker_sessions, DistributedConfig(node_id="local"))
    with worker_sessions() as session:
        session.add(TaskModel(id="root", name="Root"))
        session.commit()
    lease = manager.acquire_lease("root", "local")
    assert lease is not None

    def fail(*arguments: Any) -> bool:
        raise RuntimeError("Recovery storage failure")

    monkeypatch.setattr(manager, "_reconcile_run", fail)
    with pytest.raises(RuntimeError, match="Recovery storage failure"):
        manager.recover_lease("root", lease.lease_token)
    with worker_sessions() as session:
        stored = session.get(TaskLease, "root")
        assert stored is not None and stored.lease_token == lease.lease_token
        assert session.get(WorkflowRun, "root") is None


@pytest.mark.asyncio
async def test_worker_failure_redacts_cached_error_and_events(
    worker_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apflow.core.config_manager import get_config_manager
    from apflow.core.storage.sqlalchemy.models import TaskEvent

    secret = "synthetic-worker-failure-secret"
    monkeypatch.setenv("APFLOW_TEST_WORKER_SECRET", secret)
    configuration = get_config_manager()
    monkeypatch.setattr(configuration, "_data", dict(configuration._data))
    configuration.set(
        "security.credentials", [{"id": "test", "env_var": "APFLOW_TEST_WORKER_SECRET"}]
    )

    async def execute(task: TaskModel) -> dict[str, Any]:
        raise RuntimeError(f"Upstream failed: {secret}")

    worker = _worker(worker_sessions, execute)
    with worker_sessions() as session:
        task = TaskModel(id="failure-root", name="Root")
        session.add(task)
        session.commit()
        session.expunge(task)
    await worker._execute_task(task)
    with worker_sessions() as session:
        cached = session.query(ExecutionIdempotency).filter_by(task_id=task.id).one()
        assert "Upstream failed" in str(cached.result)
        assert secret not in str(cached.result)
        events = session.query(TaskEvent).filter_by(task_id=task.id).all()
        assert events and all(secret not in str(event.details) for event in events)
