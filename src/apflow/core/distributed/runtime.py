"""Distributed runtime coordinator with role selection state machine.

Manages the node lifecycle: registration, role selection (leader/worker/observer),
background task management (lease cleanup, node health, leader renewal),
and graceful shutdown.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy.orm import sessionmaker

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session

from apflow.core.distributed.config import DistributedConfig, utcnow as _utcnow
from apflow.core.distributed.idempotency import IdempotencyManager
from apflow.core.distributed.leader_election import LeaderElection
from apflow.core.distributed.lease_manager import LeaseManager
from apflow.core.distributed.node_registry import NodeRegistry
from apflow.core.distributed.types import TaskExecutorFn
from apflow.core.distributed.worker import WorkerRuntime
from apflow.logger import get_logger

logger = get_logger(__name__)

# Async drivers apflow may bind a session to, mapped to their synchronous
# equivalents. The distributed managers run synchronously (PostgreSQL via
# psycopg2), so an async-bound engine must be rebound to a sync driver.
_SYNC_DRIVERS = {
    "postgresql+asyncpg": "postgresql+psycopg2",
    "sqlite+aiosqlite": "sqlite",
}


def _sync_drivername(drivername: str) -> str:
    """Map an async SQLAlchemy drivername to its synchronous equivalent."""
    return _SYNC_DRIVERS.get(drivername, drivername)


def _ensure_sync_engine(bind: "Engine | Connection") -> "Engine":
    """Return a synchronous Engine for the distributed managers.

    The distributed managers use synchronous sessions (``with sf() as s: s.commit()``).
    apflow defaults ``postgresql://`` connections to the async ``asyncpg`` driver, so a
    cluster app's bound engine is typically async. Wrapping an async engine in a plain
    ``sessionmaker`` and committing synchronously raises ``MissingGreenlet``; instead we
    build a sync (psycopg2) engine over the same database. A sync engine is returned
    unchanged.
    """
    from sqlalchemy.engine import Connection as _Connection

    # session.get_bind() may return an Engine or a Connection; normalize to Engine.
    engine = bind.engine if isinstance(bind, _Connection) else bind
    if not getattr(engine.dialect, "is_async", False):
        return engine

    from sqlalchemy import create_engine

    sync_url = engine.url.set(drivername=_sync_drivername(engine.url.drivername))
    return create_engine(sync_url)


class DistributedRuntime:
    """Distributed runtime coordinator: role selection, lifecycle, background tasks.

    Manages the full distributed node lifecycle including:
    - Node registration and heartbeat
    - Role selection (auto/leader/worker/observer)
    - Leader background tasks (lease cleanup, node health checks, lease renewal)
    - Worker runtime management
    - Graceful shutdown with lease release and node deregistration
    """

    def __init__(
        self,
        config: DistributedConfig,
        session_factory: sessionmaker,
        task_executor: TaskExecutorFn | None = None,
    ) -> None:
        self._config = config
        self._node_id = config.node_id or "unknown"
        self._session_factory = session_factory

        self._node_registry = NodeRegistry(session_factory, config)
        self._leader_election = LeaderElection(session_factory, config)
        self._lease_manager = LeaseManager(session_factory, config)
        self._idempotency = IdempotencyManager(session_factory)
        self._task_executor = task_executor

        self._role: str = "initializing"
        self._lease_token: str | None = None
        self._lease_expires_at: float | None = None
        self._worker_runtime: WorkerRuntime | None = None
        # Leader-only dispatch scheduler (started when this node is leader and
        # config.scheduling_enabled). Typed loosely to avoid importing the
        # scheduler package at module load.
        self._scheduler: object | None = None
        self._background_tasks: list[asyncio.Task[None]] = []
        self._shutdown_event = asyncio.Event()

    @classmethod
    def from_session(
        cls,
        session: Session | AsyncSession,
        config: DistributedConfig,
        task_executor: TaskExecutorFn | None = None,
    ) -> "DistributedRuntime":
        """Build a runtime from an existing ORM session's bound engine.

        The distributed managers (registry, leasing, idempotency) each open their
        own short-lived sessions, so they need a ``sessionmaker`` bound to the same
        engine as ``session`` rather than the single session instance. This is the
        single construction seam callers (CLI worker, cluster bootstrap) should use
        instead of building the multi-argument constructor by hand.

        Distributed mode is synchronous (PostgreSQL via psycopg2). The app's session
        may be bound to an async engine (apflow defaults ``postgresql://`` to asyncpg),
        so the bound engine is coerced to a synchronous one via
        :func:`_ensure_sync_engine` before the managers' ``sessionmaker`` is built.

        Args:
            session: An initialized SQLAlchemy session; its bound engine is reused
                (and rebound to a sync driver if it is async).
            config: Distributed configuration.
            task_executor: Optional single-task executor. When omitted, the node
                runs as a coordinator only (leader election + lease/health loops)
                and does not execute tasks.
        """
        engine = _ensure_sync_engine(session.get_bind())
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        return cls(config, session_factory, task_executor=task_executor)

    @property
    def is_leader(self) -> bool:
        """Check if this node is currently the leader (pure read, no side effects).

        Also checks the in-memory lease expiry timestamp so a stale leader is
        immediately rejected without waiting for the next renewal loop iteration
        (fencing). Demotion and the corresponding worker startup are owned solely by
        :meth:`_leader_renewal_loop`; this getter never mutates state, so reading it
        (from a status endpoint, log line, or test) cannot silently change the role.
        """
        if self._role != "leader":
            return False
        if self._lease_expires_at is not None and _utcnow().timestamp() >= self._lease_expires_at:
            return False
        return True

    @property
    def current_role(self) -> str:
        """Return the current role of this node."""
        return self._role

    async def start(self) -> None:
        """Register node, select role, launch background tasks."""
        self._node_registry.register_node(
            node_id=self._node_id,
            executor_types=["default"],
            capabilities={},
        )
        logger.info("Node %s registered", self._node_id)

        await self._select_role()
        logger.info("Node %s selected role: %s", self._node_id, self._role)

        if self._role == "worker":
            self._start_worker_runtime()

        await self._shutdown_event.wait()

        await self._cancel_background_tasks()

    async def shutdown(self) -> None:
        """Cancel background tasks, release leadership, deregister node."""
        self._shutdown_event.set()

        await self._stop_leader_scheduler()

        if self._worker_runtime is not None:
            await self._worker_runtime.shutdown()

        if self._role == "leader" and self._lease_token is not None:
            self._leader_election.release_leadership(self._node_id, self._lease_token)
            logger.info("Leadership released by node %s", self._node_id)

        self._node_registry.deregister_node(self._node_id)
        logger.info("Node %s shut down gracefully", self._node_id)

    async def _select_role(self) -> None:
        """Role selection state machine based on config.node_role."""
        role_config = self._config.node_role

        if role_config == "observer":
            self._role = "observer"
            return

        if role_config == "worker":
            self._role = "worker"
            return

        if role_config in ("auto", "leader"):
            acquired, token = self._leader_election.try_acquire(self._node_id)

            if acquired:
                self._role = "leader"
                self._lease_token = token
                self._lease_expires_at = (
                    _utcnow() + timedelta(seconds=self._config.leader_lease_seconds)
                ).timestamp()
                self._start_leader_background_tasks()
                await self._start_leader_scheduler()
                return

            if role_config == "leader":
                raise RuntimeError(
                    f"Node {self._node_id} configured as leader but "
                    "could not acquire leadership. Another leader is active."
                )

            # auto: fall back to worker
            self._role = "worker"

    def _start_worker_runtime(self) -> None:
        """Start the worker runtime if a task executor is configured."""
        if self._task_executor is None:
            logger.warning(
                "Worker node %s has no task_executor configured; " "worker runtime will not start",
                self._node_id,
            )
            return
        self._worker_runtime = WorkerRuntime(
            node_id=self._node_id,
            config=self._config,
            session_factory=self._session_factory,
            node_registry=self._node_registry,
            lease_manager=self._lease_manager,
            idempotency_manager=self._idempotency,
            task_executor=self._task_executor,
        )
        worker_task = asyncio.create_task(self._worker_runtime.start())
        self._background_tasks.append(worker_task)

    def _start_leader_background_tasks(self) -> None:
        """Launch leader-specific background loops."""
        self._background_tasks.append(asyncio.create_task(self._leader_renewal_loop()))
        self._background_tasks.append(asyncio.create_task(self._lease_cleanup_loop()))
        self._background_tasks.append(asyncio.create_task(self._node_cleanup_loop()))

    async def _start_leader_scheduler(self) -> None:
        """Start the leader-only dispatch scheduler, if scheduling is enabled.

        The leader runs a dispatch-only scheduler: each due schedule is turned
        into a fresh pending run instance that distributed workers lease and
        execute. Because leadership is single-holder, exactly one node dispatches,
        so schedules do not fire on every node.
        """
        if not self._config.scheduling_enabled:
            return
        if self._scheduler is not None:
            return

        from apflow.scheduler.base import SchedulerConfig
        from apflow.scheduler.internal import InternalScheduler

        scheduler = InternalScheduler(
            SchedulerConfig(
                poll_interval=self._config.scheduler_poll_interval_seconds,
                max_concurrent_tasks=self._config.max_parallel_tasks_per_node,
            ),
            dispatch_only=True,
        )
        await scheduler.start()
        self._scheduler = scheduler
        logger.info("Leader node %s started dispatch-only scheduler", self._node_id)

    async def _stop_leader_scheduler(self) -> None:
        """Stop the leader-only dispatch scheduler if it is running."""
        scheduler = self._scheduler
        if scheduler is None:
            return
        self._scheduler = None
        try:
            await scheduler.stop()  # type: ignore[attr-defined]
            logger.info("Leader node %s stopped dispatch-only scheduler", self._node_id)
        except Exception:
            logger.error("Failed to stop dispatch scheduler", exc_info=True)

    async def _leader_renewal_loop(self) -> None:
        """Renew leader lease periodically. Demotes to worker on failure."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._config.leader_renew_seconds,
                )
                break
            except asyncio.TimeoutError:
                pass

            if self._lease_token is None:
                break

            success = self._leader_election.renew_leadership(self._node_id, self._lease_token)
            if success:
                self._lease_expires_at = (
                    _utcnow() + timedelta(seconds=self._config.leader_lease_seconds)
                ).timestamp()
            else:
                logger.error(
                    "Leader lease renewal failed for node %s, demoting to worker",
                    self._node_id,
                )
                # Stop dispatching before demotion so the new leader is the only
                # node turning schedules into runs.
                await self._stop_leader_scheduler()
                self._role = "worker"
                self._lease_token = None
                self._lease_expires_at = None
                self._start_worker_runtime()
                return

    async def _lease_cleanup_loop(self) -> None:
        """Clean expired leases periodically."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._config.lease_cleanup_interval_seconds,
                )
                break
            except asyncio.TimeoutError:
                pass

            try:
                self._lease_manager.cleanup_expired_leases()
            except Exception:
                logger.error("Lease cleanup failed", exc_info=True)

    async def _node_cleanup_loop(self) -> None:
        """Detect stale and dead nodes periodically."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._config.heartbeat_interval_seconds,
                )
                break
            except asyncio.TimeoutError:
                pass

            try:
                self._node_registry.detect_stale_nodes()
                self._node_registry.detect_dead_nodes()
            except Exception:
                logger.error("Node cleanup failed", exc_info=True)

    async def _cancel_background_tasks(self) -> None:
        """Cancel all background tasks and wait for completion."""
        for task in self._background_tasks:
            task.cancel()

        for task in self._background_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._background_tasks.clear()
