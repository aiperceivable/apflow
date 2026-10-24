"""Worker runtime for distributed task execution.

Implements the async polling/execution loop with lease lifecycle management,
heartbeat, idempotency checking, concurrency control, and graceful shutdown.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from sqlalchemy import text, exists, select, or_
from sqlalchemy.orm import sessionmaker

from apflow.core.distributed.config import DistributedConfig, is_postgresql, utcnow
from apflow.core.distributed.events import emit_task_event
from apflow.core.distributed.idempotency import IdempotencyManager
from apflow.core.distributed.lease_manager import LeaseManager
from apflow.core.distributed.node_registry import NodeRegistry
from apflow.core.distributed.placement import PlacementEngine
from apflow.core.distributed.types import TaskExecutorFn
from apflow.core.storage.sqlalchemy.models import DistributedNode, TaskModel, TaskDispatch
from apflow.logger import get_logger

logger = get_logger(__name__)


class WorkerRuntime:
    """Worker execution loop with lease lifecycle management.

    Polls for pending tasks, acquires leases atomically, executes tasks
    with background lease renewal, and reports results. Uses a semaphore
    to enforce max_parallel_tasks_per_node concurrency.
    """

    def __init__(
        self,
        node_id: str,
        config: DistributedConfig,
        session_factory: sessionmaker,
        node_registry: NodeRegistry,
        lease_manager: LeaseManager,
        idempotency_manager: IdempotencyManager,
        task_executor: TaskExecutorFn,
    ) -> None:
        self._node_id = node_id
        self._config = config
        self._session_factory = session_factory
        self._node_registry = node_registry
        self._lease_manager = lease_manager
        self._idempotency = idempotency_manager
        self._task_executor = task_executor
        self._semaphore = asyncio.Semaphore(config.max_parallel_tasks_per_node)
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_leases: dict[str, str] = {}  # task_id -> lease_token
        self._shutdown_event = asyncio.Event()
        self._placement_engine = PlacementEngine()

    async def start(self) -> None:
        """Start worker: register node, launch polling + heartbeat loops."""
        self._node_registry.register_node(
            node_id=self._node_id,
            executor_types=self._config.node_executor_types,
            capabilities=self._config.node_capabilities,
        )
        logger.info("Worker %s started", self._node_id)

        poll_task = asyncio.create_task(self._poll_loop())
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        await self._shutdown_event.wait()

        poll_task.cancel()
        heartbeat_task.cancel()

        for t in [poll_task, heartbeat_task]:
            try:
                await t
            except asyncio.CancelledError:
                pass

    async def shutdown(self) -> None:
        """Graceful shutdown: cancel tasks, release leases, deregister."""
        self._shutdown_event.set()

        for task_id, task in list(self._running_tasks.items()):
            task.cancel()
            logger.info("Cancelled running task: %s", task_id)

        if self._running_tasks:
            await asyncio.gather(*self._running_tasks.values(), return_exceptions=True)

        for task_id, token in list(self._active_leases.items()):
            self._lease_manager.release_lease(task_id, token)

        self._node_registry.deregister_node(self._node_id)
        logger.info("Worker %s shut down gracefully", self._node_id)

    async def _poll_loop(self) -> None:
        """Poll for executable tasks at config.poll_interval_seconds."""
        while not self._shutdown_event.is_set():
            try:
                tasks = self._find_executable_tasks()
                for task in tasks:
                    task_id = cast(str, task.id)
                    # Skip tasks already in flight: a still-pending row can be
                    # re-selected on the next poll before its status changes, and
                    # re-spawning would overwrite the live entry in _running_tasks
                    # (making _renew_lease_loop cancel the wrong coroutine).
                    if task_id in self._running_tasks:
                        continue
                    if not self._is_eligible_for_this_node(task):
                        # Leave it pending for a node whose executor_types/
                        # capabilities/allow-list actually satisfy the
                        # constraint; this is a pull model with no central
                        # dispatcher, so ineligible tasks are simply not leased.
                        continue
                    exec_task = asyncio.create_task(self._execute_task(task))
                    self._running_tasks[task_id] = exec_task
            except Exception:
                logger.error("Error in poll loop for worker %s", self._node_id, exc_info=True)

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._config.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def _heartbeat_loop(self) -> None:
        """Send heartbeat at config.heartbeat_interval_seconds."""
        while not self._shutdown_event.is_set():
            try:
                self._node_registry.heartbeat(self._node_id)
            except Exception:
                logger.error("Heartbeat failed for worker %s", self._node_id, exc_info=True)

            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._config.heartbeat_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    def _find_executable_tasks(self) -> list[TaskModel]:
        """Query for tasks that are pending and ready for execution.

        Scheduled definitions (schedule_enabled=true) are excluded: they are
        recurring templates, not runnable units. The leader's dispatch scheduler
        turns each due definition into a fresh pending run instance
        (schedule_enabled=false) — those run instances are what workers execute.

        On PostgreSQL, uses SELECT FOR UPDATE SKIP LOCKED so concurrent
        workers get non-overlapping task batches, reducing contention.
        """
        available = self._config.max_parallel_tasks_per_node - len(self._running_tasks)
        if available <= 0:
            return []
        with self._session_factory() as session:
            query = session.query(TaskModel).filter(
                TaskModel.status == "pending",
                TaskModel.schedule_enabled.isnot(True),
                TaskModel.parent_id.is_(None),
                TaskModel.cancel_requested_at.is_(None),
                or_(
                    TaskModel.origin_type.is_(None),
                    TaskModel.origin_type.notin_(["archive", "link"]),
                ),
                ~exists(select(TaskDispatch.task_id).where(TaskDispatch.task_id == TaskModel.id)),
            )
            if is_postgresql(session):
                query = query.with_for_update(skip_locked=True)
            tasks = []
            for task in query.yield_per(self._config.max_parallel_tasks_per_node):
                if str(task.id) in self._running_tasks or not self._is_eligible_for_this_node(task):
                    continue
                tasks.append(task)
                if len(tasks) >= available:
                    break
            session.expunge_all()
            return tasks

    def _is_eligible_for_this_node(self, task: TaskModel) -> bool:
        """Check task.placement_constraints against this node.

        There is no central dispatcher in this pull-based architecture — every
        worker polls the same pending pool and self-selects — so placement is
        enforced here as a self-check rather than as an external filter over
        all healthy nodes.
        """
        self_node = DistributedNode(
            node_id=self._node_id,
            executor_types=self._config.node_executor_types,
            capabilities=self._config.node_capabilities,
            status="healthy",
            heartbeat_at=utcnow(),
        )
        constraints = cast("dict[str, Any] | None", task.placement_constraints)
        eligible = self._placement_engine.find_eligible_nodes(
            constraints,
            [self_node],
            active_leases_by_node={},
            max_parallel_per_node=self._config.max_parallel_tasks_per_node,
        )
        if not eligible and constraints:
            # Make the silent-starvation case observable: a constrained task this
            # node cannot satisfy is left pending. If no node in the cluster
            # advertises the required profile it will never run — surface it rather
            # than hanging quietly. Configure APFLOW_NODE_EXECUTOR_TYPES /
            # config.node_capabilities so nodes advertise what they actually run.
            logger.warning(
                "Task %s not eligible on node %s: placement_constraints=%s vs node "
                "executor_types=%s capabilities=%s",
                task.id,
                self._node_id,
                constraints,
                self._config.node_executor_types,
                self._config.node_capabilities,
            )
        return len(eligible) > 0

    async def _execute_task(self, task: TaskModel) -> None:
        """Full task lifecycle: acquire -> check idempotency -> execute -> report."""
        task_id = cast(str, task.id)
        attempt_id = cast(int, task.attempt_id)

        async with self._semaphore:
            lease = self._lease_manager.acquire_lease(task_id, self._node_id)
            if lease is None:
                # Lost the acquire race (another worker holds it, or it completed
                # while we waited on the semaphore). _poll_loop registered this
                # task in _running_tasks before spawning us; pop it here or the
                # entry leaks forever AND _poll_loop's in-flight skip permanently
                # blocks this worker from re-attempting the task after its lease
                # expires and it reverts to pending.
                self._running_tasks.pop(task_id, None)
                return

            token = cast(str, lease.lease_token)
            self._active_leases[task_id] = token
            emit_task_event(self._session_factory, task_id, "task_assigned", self._node_id)

            # Compute the idempotency key once (incorporating the task inputs, per
            # the documented contract) so the cache-check, success-store, and
            # failure-store paths all key on the same value.
            key = IdempotencyManager.generate_key(
                task_id, attempt_id, cast("dict[str, Any] | None", task.inputs)
            )

            renewal_task = asyncio.create_task(self._renew_lease_loop(task_id, token))

            try:
                is_cached, cached_result = self._idempotency.check_cached_result(key)

                if is_cached and cached_result is not None:
                    result = cached_result
                else:
                    emit_task_event(self._session_factory, task_id, "task_started", self._node_id)
                    result = await self._execute_with_lease_context(task, token)

                if result.get("status") in ("failed", "error"):
                    self._report_failure(
                        task_id,
                        attempt_id,
                        token,
                        str(
                            result.get("error") or result.get("message") or "Task execution failed"
                        ),
                        key,
                    )
                    return
                outcome = result.get("status", "completed")
                if outcome in {"cancelled", "cancelling"}:
                    self._finalize_task(
                        task_id, token, "cancelled" if outcome == "cancelled" else "in_progress"
                    )
                    return
                if outcome != "completed":
                    logger.warning(
                        "Task %s returned nonterminal outcome %s; releasing lease", task_id, outcome
                    )
                    self._lease_manager.release_lease(task_id, token)
                    return
                if self._report_completion(task_id, token, result) and not is_cached:
                    self._idempotency.store_result(task_id, attempt_id, key, result, "completed")
            except asyncio.CancelledError:
                logger.warning("Task %s cancelled", task_id)
                # Release the lease here: the finally block pops _active_leases, so
                # shutdown()'s release loop would otherwise miss this still-held lease
                # and leave the DB row to linger until expiry.
                self._lease_manager.release_lease(task_id, token)
                raise
            except Exception as exc:
                self._report_failure(task_id, attempt_id, token, str(exc), key)
            finally:
                renewal_task.cancel()
                try:
                    await renewal_task
                except asyncio.CancelledError:
                    pass
                self._running_tasks.pop(task_id, None)
                self._active_leases.pop(task_id, None)

    async def _execute_with_lease_context(self, task: TaskModel, token: str) -> dict[str, Any]:
        """Fence every inner DAG transition with this root's live worker lease."""
        from apcore import Context, Identity
        from apflow.core.execution.context import execution_context

        current = execution_context.get()
        prior_data = getattr(current, "data", None)
        data = dict(prior_data) if isinstance(prior_data, dict) else {}
        data.update(worker_root_id=str(task.id), worker_token=token)
        identity = getattr(current, "identity", None)
        principal = getattr(task, "execution_identity", None)
        if isinstance(principal, dict):
            identity = Identity(**principal)
        context = Context.create(identity=identity, data=data)
        marker = execution_context.set(context)
        try:
            return await self._task_executor(task)
        finally:
            execution_context.reset(marker)

    async def _renew_lease_loop(self, task_id: str, lease_token: str) -> None:
        """Renew lease periodically. Stops if renewal fails."""
        interval = self._config.lease_duration_seconds / 3
        while not self._shutdown_event.is_set():
            await asyncio.sleep(interval)
            success = self._lease_manager.renew_lease(lease_token)
            if not success:
                logger.error(
                    "Lease renewal failed for task %s, cancelling",
                    task_id,
                )
                running = self._running_tasks.get(task_id)
                if running:
                    running.cancel()
                return

    def _finalize_task(self, task_id: str, lease_token: str, new_status: str) -> bool:
        """Update task status and release lease atomically, guarded by lease ownership.

        The status write is gated on the token-scoped lease DELETE matching a row: if
        the lease expired and the task was reassigned to (and possibly re-run by)
        another node, this node must not clobber the new owner's status.
        """
        with self._session_factory() as session:
            deleted = session.execute(
                text(
                    "DELETE FROM apflow_task_leases "
                    "WHERE task_id = :tid AND lease_token = :tok AND expires_at > :now "
                    "AND NOT EXISTS (SELECT 1 FROM apflow_task_dispatch WHERE task_id = :tid)"
                ),
                {"tid": task_id, "tok": lease_token, "now": utcnow()},
            )
            if deleted.rowcount == 0:
                # Lease no longer held (expired / reassigned) — skip the status write.
                session.commit()
                logger.warning(
                    "Skipped finalizing task %s: lease no longer held by this node",
                    task_id,
                )
                return False

            task = session.get(TaskModel, task_id)
            if task is not None and task.status not in {"completed", "failed", "cancelled"}:
                task.status = "cancelled" if task.cancel_requested_at is not None else new_status
            session.commit()
            return task is not None and task.status == new_status

    def _report_completion(self, task_id: str, lease_token: str, result: dict[str, Any]) -> bool:
        """Mark task as completed only while its execution lease is still owned."""
        if not self._finalize_task(task_id, lease_token, "completed"):
            return False
        emit_task_event(self._session_factory, task_id, "task_completed", self._node_id)
        logger.info("Task %s completed", task_id)
        return True

    def _report_failure(
        self, task_id: str, attempt_id: int, lease_token: str, error: str, key: str
    ) -> None:
        """Mark task as failed, release lease, store failure, and emit event."""
        if not self._finalize_task(task_id, lease_token, "failed"):
            return
        self._idempotency.store_failure(
            task_id,
            attempt_id,
            key,
            {"error": error},
        )
        emit_task_event(
            self._session_factory,
            task_id,
            "task_failed",
            self._node_id,
            {"error": error},
        )
        logger.info("Task %s failed: %s", task_id, error)
