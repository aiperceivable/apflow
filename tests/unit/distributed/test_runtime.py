"""Tests for DistributedRuntime coordinator with role selection."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sqlalchemy import create_engine, make_url
from sqlalchemy.orm import sessionmaker

from apflow.core.distributed.config import DistributedConfig, utcnow as _utcnow
from apflow.core.distributed.runtime import (
    DistributedRuntime,
    _ensure_sync_engine,
    _sync_drivername,
)


def _make_config(**overrides: Any) -> DistributedConfig:
    """Create a test DistributedConfig with fast intervals."""
    defaults = {
        "enabled": True,
        "node_id": "test-node",
        "node_role": "auto",
        "poll_interval_seconds": 0.05,
        "heartbeat_interval_seconds": 0.05,
        "lease_duration_seconds": 30,
        "leader_renew_seconds": 0.05,
        "leader_lease_seconds": 30,
        "lease_cleanup_interval_seconds": 0.05,
        "max_parallel_tasks_per_node": 2,
    }
    defaults.update(overrides)
    return DistributedConfig(**defaults)


def _make_runtime(
    config: DistributedConfig | None = None,
    node_registry: MagicMock | None = None,
    leader_election: MagicMock | None = None,
    lease_manager: MagicMock | None = None,
    idempotency_manager: MagicMock | None = None,
    session_factory: MagicMock | None = None,
    task_executor: AsyncMock | None = None,
) -> DistributedRuntime:
    """Create a DistributedRuntime with injected mock dependencies."""
    cfg = config or _make_config()
    runtime = DistributedRuntime.__new__(DistributedRuntime)
    runtime._config = cfg
    runtime._node_id = cfg.node_id or "test-node"
    runtime._session_factory = session_factory or MagicMock()
    runtime._node_registry = node_registry or MagicMock()
    runtime._leader_election = leader_election or MagicMock()
    runtime._lease_manager = lease_manager or MagicMock()
    runtime._idempotency = idempotency_manager or MagicMock()
    runtime._task_executor = task_executor or AsyncMock(return_value={"status": "completed"})
    runtime._role = "initializing"
    runtime._lease_token = None
    runtime._lease_expires_at = None
    runtime._worker_runtime = None
    runtime._scheduler = None
    runtime._background_tasks = []
    runtime._shutdown_event = asyncio.Event()
    return runtime


class TestFromSessionEngineCoercion:
    """from_session must hand the sync distributed managers a sync engine.

    apflow defaults ``postgresql://`` to the async asyncpg driver, so a cluster
    app's bound engine is async; wrapping it in a sync sessionmaker would raise
    MissingGreenlet at the first commit.
    """

    def test_sync_drivername_maps_async_to_sync(self) -> None:
        assert _sync_drivername("postgresql+asyncpg") == "postgresql+psycopg2"
        assert _sync_drivername("sqlite+aiosqlite") == "sqlite"

    def test_sync_drivername_leaves_sync_unchanged(self) -> None:
        assert _sync_drivername("postgresql+psycopg2") == "postgresql+psycopg2"
        assert _sync_drivername("sqlite") == "sqlite"

    def test_ensure_sync_engine_passes_through_sync_engine(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        assert _ensure_sync_engine(engine) is engine

    def test_ensure_sync_engine_rebinds_async_engine_to_sync(self) -> None:
        # Build a stand-in for an async-bound engine without requiring the async
        # driver: a stub whose dialect reports is_async and whose url carries an
        # async drivername (sqlite+aiosqlite -> sqlite keeps the test driver-free).
        class _StubDialect:
            is_async = True

        class _StubAsyncEngine:
            dialect = _StubDialect()
            url = make_url("sqlite+aiosqlite:///:memory:")

        result = _ensure_sync_engine(_StubAsyncEngine())  # type: ignore[arg-type]

        assert result.dialect.is_async is False
        assert "aiosqlite" not in result.url.drivername

    def test_from_session_builds_sync_factory(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        session = sessionmaker(bind=engine)()
        try:
            runtime = DistributedRuntime.from_session(session, _make_config())
            produced = runtime._session_factory()
            try:
                assert produced.get_bind().dialect.is_async is False
            finally:
                produced.close()
        finally:
            session.close()


class TestRoleSelection:
    """Tests for role selection state machine."""

    @pytest.mark.asyncio
    async def test_auto_role_attempts_leader_first(self) -> None:
        """node_role='auto': try_acquire is called; on success, role becomes 'leader'."""
        election = MagicMock()
        election.try_acquire.return_value = (True, "lease-token-abc")

        runtime = _make_runtime(
            config=_make_config(node_role="auto"),
            leader_election=election,
        )

        await runtime._select_role()

        election.try_acquire.assert_called_once_with("test-node")
        assert runtime.current_role == "leader"
        assert runtime.is_leader is True

    @pytest.mark.asyncio
    async def test_auto_role_falls_back_to_worker(self) -> None:
        """node_role='auto': if try_acquire fails, role becomes 'worker'."""
        election = MagicMock()
        election.try_acquire.return_value = (False, None)

        runtime = _make_runtime(
            config=_make_config(node_role="auto"),
            leader_election=election,
        )

        await runtime._select_role()

        assert runtime.current_role == "worker"
        assert runtime.is_leader is False

    @pytest.mark.asyncio
    async def test_leader_role_succeeds_on_acquire(self) -> None:
        """node_role='leader': becomes leader if acquire succeeds."""
        election = MagicMock()
        election.try_acquire.return_value = (True, "lease-token-xyz")

        runtime = _make_runtime(
            config=_make_config(node_role="leader"),
            leader_election=election,
        )

        await runtime._select_role()

        assert runtime.current_role == "leader"

    @pytest.mark.asyncio
    async def test_leader_role_fails_if_cannot_acquire(self) -> None:
        """node_role='leader': raises RuntimeError if leadership acquisition fails."""
        election = MagicMock()
        election.try_acquire.return_value = (False, None)

        runtime = _make_runtime(
            config=_make_config(node_role="leader"),
            leader_election=election,
        )

        with pytest.raises(RuntimeError, match="could not acquire leadership"):
            await runtime._select_role()

    @pytest.mark.asyncio
    async def test_worker_role_never_attempts_leadership(self) -> None:
        """node_role='worker': try_acquire is never called."""
        election = MagicMock()

        runtime = _make_runtime(
            config=_make_config(node_role="worker"),
            leader_election=election,
        )

        await runtime._select_role()

        election.try_acquire.assert_not_called()
        assert runtime.current_role == "worker"

    @pytest.mark.asyncio
    async def test_observer_role_read_only(self) -> None:
        """node_role='observer': no leader election, no worker polling."""
        election = MagicMock()

        runtime = _make_runtime(
            config=_make_config(node_role="observer"),
            leader_election=election,
        )

        await runtime._select_role()

        election.try_acquire.assert_not_called()
        assert runtime.current_role == "observer"
        assert runtime.is_leader is False


class TestLifecycle:
    """Tests for start/shutdown lifecycle."""

    @pytest.mark.asyncio
    async def test_start_registers_node_and_selects_role(self) -> None:
        """start() registers node and selects role."""
        registry = MagicMock()
        election = MagicMock()
        election.try_acquire.return_value = (False, None)

        runtime = _make_runtime(
            config=_make_config(node_role="observer"),
            node_registry=registry,
            leader_election=election,
        )

        task = asyncio.create_task(runtime.start())
        await asyncio.sleep(0.1)
        await runtime.shutdown()
        await task

        registry.register_node.assert_called_once()
        assert runtime.current_role == "observer"

    @pytest.mark.asyncio
    async def test_shutdown_sets_event(self) -> None:
        """shutdown() sets the shutdown event."""
        runtime = _make_runtime(config=_make_config(node_role="observer"))

        task = asyncio.create_task(runtime.start())
        await asyncio.sleep(0.1)
        await runtime.shutdown()
        await task

        assert runtime._shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_shutdown_deregisters_node(self) -> None:
        """shutdown() deregisters node from registry."""
        registry = MagicMock()
        runtime = _make_runtime(
            config=_make_config(node_role="observer"),
            node_registry=registry,
        )

        task = asyncio.create_task(runtime.start())
        await asyncio.sleep(0.1)
        await runtime.shutdown()
        await task

        registry.deregister_node.assert_called_once_with("test-node")

    @pytest.mark.asyncio
    async def test_shutdown_releases_leadership(self) -> None:
        """shutdown() releases leadership if node was leader."""
        election = MagicMock()
        election.try_acquire.return_value = (True, "token-abc")

        runtime = _make_runtime(
            config=_make_config(node_role="leader"),
            leader_election=election,
        )

        task = asyncio.create_task(runtime.start())
        await asyncio.sleep(0.1)
        await runtime.shutdown()
        await task

        election.release_leadership.assert_called_once_with("test-node", "token-abc")


class TestLeaderBackgroundTasks:
    """Tests for leader-specific background tasks."""

    @pytest.mark.asyncio
    async def test_leader_runs_lease_cleanup_loop(self) -> None:
        """Leader runs cleanup_expired_leases periodically."""
        election = MagicMock()
        election.try_acquire.return_value = (True, "token")
        election.renew_leadership.return_value = True
        lease_mgr = MagicMock()

        runtime = _make_runtime(
            config=_make_config(node_role="leader"),
            leader_election=election,
            lease_manager=lease_mgr,
        )

        task = asyncio.create_task(runtime.start())
        await asyncio.sleep(0.2)
        await runtime.shutdown()
        await task

        assert lease_mgr.cleanup_expired_leases.call_count >= 1

    @pytest.mark.asyncio
    async def test_leader_runs_node_cleanup_loop(self) -> None:
        """Leader detects stale and dead nodes periodically."""
        election = MagicMock()
        election.try_acquire.return_value = (True, "token")
        election.renew_leadership.return_value = True
        registry = MagicMock()

        runtime = _make_runtime(
            config=_make_config(node_role="leader"),
            leader_election=election,
            node_registry=registry,
        )

        task = asyncio.create_task(runtime.start())
        await asyncio.sleep(0.2)
        await runtime.shutdown()
        await task

        assert registry.detect_stale_nodes.call_count >= 1
        assert registry.detect_dead_nodes.call_count >= 1

    @pytest.mark.asyncio
    async def test_leader_renewal_loop(self) -> None:
        """Leader renews its own leadership lease periodically."""
        election = MagicMock()
        election.try_acquire.return_value = (True, "token")
        election.renew_leadership.return_value = True

        runtime = _make_runtime(
            config=_make_config(node_role="leader"),
            leader_election=election,
        )

        task = asyncio.create_task(runtime.start())
        await asyncio.sleep(0.2)
        await runtime.shutdown()
        await task

        assert election.renew_leadership.call_count >= 1

    @pytest.mark.asyncio
    async def test_leader_renewal_failure_demotes_to_worker(self) -> None:
        """If leader lease renewal fails, runtime transitions to worker role."""
        election = MagicMock()
        election.try_acquire.return_value = (True, "token")
        election.renew_leadership.return_value = False

        runtime = _make_runtime(
            config=_make_config(node_role="leader"),
            leader_election=election,
        )

        task = asyncio.create_task(runtime.start())
        await asyncio.sleep(0.2)
        await runtime.shutdown()
        await task

        assert runtime.current_role == "worker"

    @pytest.mark.asyncio
    async def test_demotion_stops_lease_and_node_cleanup_loops(self) -> None:
        """Regression: demotion stopped the dispatch scheduler but left
        _lease_cleanup_loop/_node_cleanup_loop running indefinitely — a
        demoted node kept performing exclusive leader-only DB maintenance
        after losing leadership. (Review CRITICAL #23)
        """
        election = MagicMock()
        election.try_acquire.return_value = (True, "token")
        election.renew_leadership.return_value = False  # fails immediately
        lease_mgr = MagicMock()
        registry = MagicMock()

        runtime = _make_runtime(
            config=_make_config(node_role="leader"),
            leader_election=election,
            lease_manager=lease_mgr,
            node_registry=registry,
        )

        task = asyncio.create_task(runtime.start())
        await asyncio.sleep(0.2)  # allow renewal failure + demotion to occur
        assert runtime.current_role == "worker"

        cleanup_calls_at_demotion = lease_mgr.cleanup_expired_leases.call_count
        node_cleanup_calls_at_demotion = registry.detect_stale_nodes.call_count

        # Give the (buggy) loops ample opportunity to keep firing.
        await asyncio.sleep(0.3)

        await runtime.shutdown()
        await task

        assert lease_mgr.cleanup_expired_leases.call_count == cleanup_calls_at_demotion
        assert registry.detect_stale_nodes.call_count == node_cleanup_calls_at_demotion


class TestProperties:
    """Tests for runtime properties."""

    @pytest.mark.asyncio
    async def test_is_leader_property(self) -> None:
        """is_leader returns True only when role is 'leader' with a valid lease."""
        runtime = _make_runtime()
        future = (_utcnow() + timedelta(seconds=300)).timestamp()

        runtime._role = "leader"
        runtime._lease_expires_at = future
        assert runtime.is_leader is True

        runtime._role = "worker"
        assert runtime.is_leader is False

        runtime._role = "observer"
        assert runtime.is_leader is False

    @pytest.mark.asyncio
    async def test_is_leader_expired_lease_fences_without_mutating(self) -> None:
        """is_leader returns False on an expired lease (fencing) but is a pure read:
        demotion + worker startup are owned by the renewal loop, not this getter."""
        runtime = _make_runtime()
        past = (_utcnow() - timedelta(seconds=10)).timestamp()

        runtime._role = "leader"
        runtime._lease_token = "token"
        runtime._lease_expires_at = past

        assert runtime.is_leader is False
        # State is left untouched — the renewal loop performs the real transition.
        assert runtime.current_role == "leader"
        assert runtime._lease_token == "token"
        assert runtime._lease_expires_at == past

    @pytest.mark.asyncio
    async def test_current_role_property(self) -> None:
        """current_role returns the active role string."""
        runtime = _make_runtime()

        runtime._role = "leader"
        assert runtime.current_role == "leader"

        runtime._role = "worker"
        assert runtime.current_role == "worker"

        runtime._role = "observer"
        assert runtime.current_role == "observer"


@pytest.fixture
def runtime_no_executor() -> DistributedRuntime:
    """A DistributedRuntime with no task_executor configured."""
    runtime = _make_runtime(task_executor=None)
    runtime._task_executor = None
    return runtime


class TestWorkerRuntimeStart:
    """Tests for _start_worker_runtime behavior."""

    def test_no_executor_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """_start_worker_runtime logs warning when task_executor is None."""
        runtime = _make_runtime(task_executor=None)
        runtime._task_executor = None

        with caplog.at_level(logging.WARNING, logger="apflow.core.distributed.runtime"):
            runtime._start_worker_runtime()

        assert runtime._worker_runtime is None
        assert "no task_executor configured" in caplog.text

    def test_start_worker_runtime_without_executor_logs_warning(
        self, runtime_no_executor: DistributedRuntime, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Worker runtime is not started when no task_executor is configured."""
        with caplog.at_level(logging.WARNING, logger="apflow.core.distributed.runtime"):
            runtime_no_executor._start_worker_runtime()

        assert runtime_no_executor._worker_runtime is None
        assert "no task_executor configured" in caplog.text


class TestDistributedRuntimeFromSession:
    """Construction-seam tests for ``DistributedRuntime.from_session``.

    These exercise the REAL ``__init__`` contract (not ``__new__``), which is the
    contract the ``apflow worker`` command and cluster bootstrap must satisfy.
    A regression here is exactly what made ``WorkerRuntime(config)`` /
    ``DistributedRuntime(dist_config)`` raise ``TypeError`` at the call sites.

    They build their own SQLite session so they do not depend on the (optionally
    PostgreSQL-bound) shared ``session`` fixture — construction never touches the DB.
    """

    @pytest.fixture
    def sqlite_session(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine("sqlite:///:memory:")
        sess = sessionmaker(bind=engine)()
        try:
            yield sess
        finally:
            sess.close()
            engine.dispose()

    def test_from_session_builds_runtime_with_full_contract(self, sqlite_session):
        """from_session wires every collaborator via a real constructor call."""
        config = _make_config()
        runtime = DistributedRuntime.from_session(sqlite_session, config)

        assert runtime._config is config
        assert runtime._task_executor is None
        # All managers wired — proves __init__ (not __new__) ran successfully.
        assert runtime._node_registry is not None
        assert runtime._leader_election is not None
        assert runtime._lease_manager is not None
        assert runtime._idempotency is not None
        assert runtime._role == "initializing"

    def test_from_session_reuses_source_engine(self, sqlite_session):
        """The derived session_factory is bound to the source session's engine."""
        runtime = DistributedRuntime.from_session(sqlite_session, _make_config())

        derived = runtime._session_factory()
        try:
            assert derived.get_bind() is sqlite_session.get_bind()
        finally:
            derived.close()

    def test_from_session_passes_task_executor(self, sqlite_session):
        """A provided task_executor is forwarded to the runtime."""

        async def executor(_task: Any) -> dict[str, Any]:
            return {"status": "completed"}

        runtime = DistributedRuntime.from_session(
            sqlite_session, _make_config(), task_executor=executor
        )
        assert runtime._task_executor is executor


class TestIsLeaderIsPure:
    """is_leader is a pure read: it never mutates role/lease state."""

    def test_expired_lease_reports_not_leader_without_mutating(self) -> None:
        runtime = _make_runtime(config=_make_config())
        runtime._role = "leader"
        runtime._lease_token = "tok"
        runtime._lease_expires_at = _utcnow().timestamp() - 1.0  # already expired

        assert runtime.is_leader is False
        # Reading the property must not demote the node or clear the lease.
        assert runtime._role == "leader"
        assert runtime._lease_token == "tok"

    def test_valid_lease_reports_leader(self) -> None:
        runtime = _make_runtime(config=_make_config())
        runtime._role = "leader"
        runtime._lease_expires_at = _utcnow().timestamp() + 100.0
        assert runtime.is_leader is True


class TestLeaderScheduler:
    """Leader-side dispatch scheduler wiring."""

    @pytest.mark.asyncio
    async def test_scheduler_not_started_when_disabled(self) -> None:
        runtime = _make_runtime(_make_config(scheduling_enabled=False))
        await runtime._start_leader_scheduler()
        assert runtime._scheduler is None

    @pytest.mark.asyncio
    async def test_scheduler_started_dispatch_only_and_stopped(self) -> None:
        runtime = _make_runtime(_make_config(scheduling_enabled=True))
        fake = MagicMock()
        fake.start = AsyncMock()
        fake.stop = AsyncMock()

        with patch("apflow.scheduler.internal.InternalScheduler", return_value=fake) as ctor:
            await runtime._start_leader_scheduler()
            # Must construct the scheduler in dispatch-only mode.
            assert ctor.call_args.kwargs.get("dispatch_only") is True
            fake.start.assert_awaited_once()
            assert runtime._scheduler is fake

            await runtime._stop_leader_scheduler()
            fake.stop.assert_awaited_once()
            assert runtime._scheduler is None
