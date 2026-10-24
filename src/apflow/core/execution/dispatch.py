"""Persistent root-task acceptance and lease-based background execution."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import uuid4

from apcore import Context, Identity
from apcore.errors import ModuleError
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy_session_proxy import SqlalchemySessionProxy

from apflow.core.execution.context import execution_context
from apflow.core.storage.session import session_scope
from apflow.core.storage.sqlalchemy.models import (
    ExecutionAttempt,
    TaskDispatch,
    TaskLease,
    TaskModel,
    WorkflowRun,
)
from apflow.logger import get_logger

logger = get_logger(__name__)


async def database_run_exists(session: Any, task_id: str) -> bool:
    database = SqlalchemySessionProxy(session)
    return (
        await database.execute(select(WorkflowRun.id).where(WorkflowRun.id == task_id))
    ).first() is not None


async def accept_task(session: Any, task_id: str, context: Any) -> dict[str, Any]:
    """Commit acceptance before returning and release admission locks on errors."""
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 255:
        raise ModuleError(code="INVALID_INPUT", message="task_id must contain 1 to 255 characters")
    database = SqlalchemySessionProxy(session)
    try:
        return await _accept_locked(database, session, task_id, context)
    except Exception:
        await database.rollback()
        raise


async def _validate_submission_root(database: Any, session: Any, task_id: str) -> None:
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    leased = (
        await database.execute(
            select(TaskLease.task_id).where(
                TaskLease.task_id == task_id,
                TaskLease.expires_at > datetime.now(timezone.utc),
            )
        )
    ).first()
    if leased is not None:
        raise ModuleError(code="TASK_IMMUTABLE", message="Task is owned by a distributed worker")
    task = (
        await database.execute(select(TaskModel).where(TaskModel.id == task_id))
    ).scalar_one_or_none()
    if task is None or task.parent_id is not None:
        raise ModuleError(code="INVALID_INPUT", message="Submit an existing root task")
    run = (
        await database.execute(
            select(WorkflowRun).where(WorkflowRun.id == task_id, WorkflowRun.status == "accepted")
        )
    ).scalar_one_or_none()
    frozen_retry_root = (
        run is not None
        and task.status == "completed"
        and task.origin_type == "link"
        and task.result_snapshot_hash is not None
    )
    if not frozen_retry_root and (
        task.status != "pending" or task.origin_type in ("archive", "link")
    ):
        raise ModuleError(
            code="INVALID_INPUT", message="Only a pending executable root may be submitted"
        )
    rows = await TaskRepository(session).get_all_tasks_in_tree(task)
    if any(
        child.status == "in_progress" or child.cancel_requested_at is not None for child in rows
    ):
        raise ModuleError(
            code="TASK_IMMUTABLE", message="Task tree has an active or cancelled execution"
        )


async def _accept_locked(database: Any, session: Any, task_id: str, context: Any) -> dict[str, Any]:
    from sqlalchemy.exc import IntegrityError

    # Serialize admission with both local claims and distributed worker leases.
    await database.execute(
        update(TaskModel)
        .where(TaskModel.id == task_id)
        .values(id=TaskModel.id, updated_at=TaskModel.updated_at)
    )
    existing = (
        await database.execute(select(TaskDispatch).where(TaskDispatch.task_id == task_id))
    ).scalar_one_or_none()
    if existing is not None:
        await database.commit()
        return {"task_id": task_id, "state": existing.state}
    await _validate_submission_root(database, session, task_id)
    from apflow.core.execution.runs import RunService

    run = await RunService(session).ensure_run(task_id, commit=False)
    if run.status != "accepted":
        raise ModuleError(code="RUN_NOT_EXECUTABLE", message="Create a new run to retry")
    identity = getattr(context, "identity", None)
    principal = (
        None
        if identity is None
        else {
            "id": identity.id,
            "type": identity.type,
            "roles": list(identity.roles),
            "attrs": {
                "apflow_namespace": getattr(context, "data", {}).get("apflow_namespace", "apflow")
            },
        }
    )
    database.add(TaskDispatch(task_id=task_id, state="accepted", identity=principal))
    try:
        await database.commit()
    except IntegrityError:
        await database.rollback()
        existing = (
            await database.execute(select(TaskDispatch).where(TaskDispatch.task_id == task_id))
        ).scalar_one()
        return {"task_id": task_id, "state": existing.state}
    return {"task_id": task_id, "state": "accepted"}


class TaskDispatcher:
    """Run only accepted roots; uncertain interrupted side effects require reconciliation."""

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        execute: Any = None,
        lease_seconds: float = 30,
        poll_seconds: float = 1,
        max_concurrent: int = 10,
    ) -> None:
        if lease_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("Dispatcher intervals must be positive")
        if (
            isinstance(max_concurrent, bool)
            or not isinstance(max_concurrent, int)
            or max_concurrent <= 0
        ):
            raise ValueError("max_concurrent must be a positive integer")
        self.factory = factory
        self.execute = execute or self._execute_task
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.max_concurrent = max_concurrent
        self._poll: asyncio.Task[None] | None = None
        self._running: dict[str, asyncio.Task[None]] = {}

    async def _execute_task(self, task_id: str, session: Any, context: Any) -> dict[str, Any]:
        from apflow.core.execution.task_executor import TaskExecutor

        return await TaskExecutor().execute_task_by_id(task_id, db_session=session)

    async def start(self) -> None:
        if self._poll is not None:
            raise RuntimeError("Dispatcher is already running")
        self._poll = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        tasks = [*self._running.values()]
        if self._poll is not None:
            tasks.append(self._poll)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()
        self._poll = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._dispatch()
            except Exception:
                logger.exception("Background dispatch poll failed")
            await asyncio.sleep(self.poll_seconds)

    async def run_once(self) -> None:
        """Dispatch eligible work and wait for it, primarily for bounded workers/tests."""
        await self._dispatch()
        await asyncio.gather(*list(self._running.values()))

    async def _recover(self, database: Any) -> None:
        now = datetime.now(timezone.utc)
        rows = (
            (
                await database.execute(
                    select(TaskDispatch).where(
                        TaskDispatch.state.in_(["running", "cancel_requested"]),
                        TaskDispatch.lease_expires_at < now,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            task = (
                await database.execute(select(TaskModel).where(TaskModel.id == row.task_id))
            ).scalar_one_or_none()
            state = "reconciliation_required"
            tasks: list[TaskModel] = []
            if task is not None:
                from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

                tasks = await TaskRepository(database._session).get_all_tasks_in_tree(task)
                invocation_started = (
                    await database.execute(
                        select(ExecutionAttempt.id)
                        .where(ExecutionAttempt.task_id.in_([child.id for child in tasks]))
                        .limit(1)
                    )
                ).first() is not None
                uncertain = invocation_started or any(
                    child.status == "in_progress"
                    or (child.error_info or {}).get("delivery_state") == "unknown"
                    or child.started_at is not None
                    for child in tasks
                )
                if all(child.status == "completed" for child in tasks):
                    state = "finished"
                elif not uncertain and all(
                    child.status == "pending" or child.origin_type == "link" for child in tasks
                ):
                    state = "accepted"
            changed = await database.execute(
                update(TaskDispatch)
                .where(
                    TaskDispatch.task_id == row.task_id,
                    TaskDispatch.owner_token == row.owner_token,
                    TaskDispatch.lease_expires_at < now,
                )
                .values(state=state, owner_token=None, lease_expires_at=None)
                .execution_options(synchronize_session=False)
            )
            if (
                cast(CursorResult, changed).rowcount
                and state == "reconciliation_required"
                and task is not None
            ):
                identifiers = [child.id for child in tasks]
                from apflow.governance.ledger import BudgetLedger
                from apflow.core.storage.sqlalchemy.models import BudgetReservation

                reservations = (
                    (
                        await database.execute(
                            select(BudgetReservation.id).where(
                                BudgetReservation.task_id.in_(identifiers),
                                BudgetReservation.state == "reserved",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                ledger = BudgetLedger(database._session)
                for reservation_id in reservations:
                    await ledger.settle(reservation_id, None, commit=False)
                await database.execute(
                    update(ExecutionAttempt)
                    .where(
                        ExecutionAttempt.task_id.in_(identifiers),
                        ExecutionAttempt.status == "running",
                    )
                    .values(status="unknown", completed_at=now)
                    .execution_options(synchronize_session=False)
                )
                await database.execute(
                    update(WorkflowRun)
                    .where(
                        WorkflowRun.id == row.task_id,
                        WorkflowRun.status.in_(["accepted", "running"]),
                    )
                    .values(status="reconciliation_required", completed_at=now)
                )
            elif cast(CursorResult, changed).rowcount and state == "accepted":
                # A claimed run with no invocation may be admitted after a process crash.
                await database.execute(
                    update(WorkflowRun)
                    .where(
                        WorkflowRun.id == row.task_id,
                        WorkflowRun.status == "running",
                    )
                    .values(status="accepted", started_at=None)
                )
            elif cast(CursorResult, changed).rowcount and state == "finished":
                await database.execute(
                    update(WorkflowRun)
                    .where(
                        WorkflowRun.id == row.task_id,
                        WorkflowRun.status.in_(["accepted", "running"]),
                    )
                    .values(status="completed", completed_at=now)
                )
        await database.commit()

    async def _dispatch(self) -> None:
        async with session_scope(self.factory) as session:
            database = SqlalchemySessionProxy(session)
            await self._recover(database)
            capacity = self.max_concurrent - len(self._running)
            if capacity <= 0:
                return
            rows = (
                (
                    await database.execute(
                        select(TaskDispatch)
                        .where(TaskDispatch.state == "accepted")
                        .limit(min(20, capacity))
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                if row.task_id in self._running:
                    continue
                token = uuid4().hex
                claimed = await database.execute(
                    update(TaskDispatch)
                    .where(
                        TaskDispatch.task_id == row.task_id,
                        TaskDispatch.state == "accepted",
                    )
                    .values(
                        state="running",
                        owner_token=token,
                        attempt_id=TaskDispatch.attempt_id + 1,
                        lease_expires_at=datetime.now(timezone.utc)
                        + timedelta(seconds=self.lease_seconds),
                    )
                )
                await database.commit()
                if cast(CursorResult, claimed).rowcount:
                    task_id, principal = row.task_id, row.identity
                    running = asyncio.create_task(self._run(task_id, token, principal))
                    self._running[task_id] = running
                    running.add_done_callback(
                        lambda _, identifier=task_id: self._running.pop(identifier, None)
                    )

    async def _renew(self, task_id: str, token: str) -> None:
        while True:
            await asyncio.sleep(self.lease_seconds / 3)
            async with session_scope(self.factory) as session:
                database = SqlalchemySessionProxy(session)
                changed = await database.execute(
                    update(TaskDispatch)
                    .where(
                        TaskDispatch.task_id == task_id,
                        TaskDispatch.owner_token == token,
                        TaskDispatch.state.in_(["running", "cancel_requested"]),
                        TaskDispatch.lease_expires_at > datetime.now(timezone.utc),
                    )
                    .values(
                        lease_expires_at=datetime.now(timezone.utc)
                        + timedelta(seconds=self.lease_seconds)
                    )
                )
                await database.commit()
                if not cast(CursorResult, changed).rowcount:
                    raise RuntimeError("Background execution lease was lost")
                task = (
                    await database.execute(select(TaskModel).where(TaskModel.id == task_id))
                ).scalar_one()
                if task.cancel_requested_at:
                    from apflow.core.execution.task_executor import TaskExecutor

                    await TaskExecutor().cancel_task_tree(task_id, db_session=session)

    async def _run(self, task_id: str, token: str, principal: dict[str, Any] | None) -> None:
        identity = None if principal is None else Identity(**principal)
        namespace = (principal or {}).get("attrs", {}).get("apflow_namespace", "apflow")
        context = Context.create(
            identity=identity,
            data={
                "dispatch_task_id": task_id,
                "dispatch_token": token,
                "apflow_namespace": namespace,
            },
        )
        renewal = asyncio.create_task(self._renew(task_id, token))
        work: asyncio.Task[Any] | None = None
        state = "finished"
        try:
            async with session_scope(self.factory) as session:
                context_token = execution_context.set(context)
                try:
                    work = asyncio.create_task(self.execute(task_id, session, context))
                    done, _ = await asyncio.wait(
                        [work, renewal], return_when=asyncio.FIRST_COMPLETED
                    )
                    if renewal in done:
                        work.cancel()
                        await asyncio.gather(work, return_exceptions=True)
                        await renewal
                    await work
                    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

                    repository = TaskRepository(session)
                    root = await repository.get_task_by_id(task_id)
                    rows = await repository.get_all_tasks_in_tree(root) if root is not None else []
                    if any(
                        task.status == "in_progress"
                        or (task.error_info or {}).get("delivery_state") == "unknown"
                        for task in rows
                    ):
                        state = "reconciliation_required"
                    from apflow.core.execution.runs import RunService

                    if await database_run_exists(session, task_id):
                        final_status = await RunService(session).finish_from_tasks(task_id)
                        if final_status == "reconciliation_required":
                            state = "reconciliation_required"
                finally:
                    execution_context.reset(context_token)
        except asyncio.CancelledError:
            if work is not None:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            raise
        except Exception:
            state = "reconciliation_required"
            logger.exception("Background execution interrupted for task %s", task_id)
        finally:
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                await renewal
        async with session_scope(self.factory) as session:
            database = SqlalchemySessionProxy(session)
            await database.execute(
                update(TaskDispatch)
                .where(
                    TaskDispatch.task_id == task_id,
                    TaskDispatch.owner_token == token,
                )
                .values(state=state, owner_token=None, lease_expires_at=None)
            )
            await database.commit()
