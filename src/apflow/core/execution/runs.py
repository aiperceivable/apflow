"""Explicit run lifecycle and immutable, sanitized graph definition snapshots."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, cast

from apcore.errors import ModuleError
from sqlalchemy import exists, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy_session_proxy import SqlalchemySessionProxy

from apflow.core.storage.sqlalchemy.models import (
    ExecutionAttempt,
    TaskDispatch,
    TaskLease,
    TaskModel,
    WorkflowRun,
)
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.core.utils.credentials import redact_public_data

TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "reconciliation_required"})


def summarize_run(tasks: list[TaskModel]) -> str:
    """Summarize all nodes, including children of a completed structural root."""
    if any((task.error_info or {}).get("delivery_state") == "unknown" for task in tasks):
        return "reconciliation_required"
    if any(task.status == "pending" and task.started_at is not None for task in tasks):
        return "reconciliation_required"
    statuses = {task.status for task in tasks}
    if "in_progress" in statuses:
        return "reconciliation_required"
    if "failed" in statuses:
        return "failed"
    if "cancelled" in statuses:
        return "cancelled"
    if statuses == {"completed"}:
        return "completed"
    return "accepted"


def snapshot_run_definition(tasks: list[TaskModel]) -> dict[str, Any]:
    """Build the same sanitized executable snapshot for async admission and recovery."""
    fields = (
        "id",
        "name",
        "parent_id",
        "dependencies",
        "inputs",
        "params",
        "priority",
        "origin_type",
        "original_task_id",
        "source_task_id",
        "result_snapshot_hash",
        "schemas",
        "budget_scope_id",
        "token_budget",
        "cost_policy",
        "estimated_cost_usd",
        "max_attempts",
        "backoff_strategy",
        "backoff_base_seconds",
        "placement_constraints",
    )
    snapshot = {
        "version": 1,
        "tasks": [
            redact_public_data({field: getattr(task, field, None) for field in fields})
            for task in sorted(tasks, key=lambda row: row.id)
        ],
    }
    return json.loads(json.dumps(snapshot, default=str, allow_nan=False))


def new_run_record(
    root: TaskModel, tasks: list[TaskModel], *, retry_of_run_id: str | None = None
) -> WorkflowRun:
    """Construct a snapshot record; callers bind nodes and own the transaction."""
    snapshot = snapshot_run_definition(tasks)
    serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False)
    status = summarize_run(tasks)
    return WorkflowRun(
        id=root.id,
        root_task_id=root.id,
        user_id=root.user_id,
        status=status,
        definition_snapshot=snapshot,
        definition_hash=hashlib.sha256(serialized.encode()).hexdigest(),
        retry_of_run_id=retry_of_run_id,
        completed_at=datetime.now(timezone.utc) if status in TERMINAL_RUN_STATUSES else None,
    )


class RunService:
    """Bind existing node records to one run without rewriting historical execution."""

    def __init__(self, session: Session | AsyncSession) -> None:
        self.database = SqlalchemySessionProxy(session)
        self.repository = TaskRepository(session)

    def _ownership_conditions(self, run_id: str) -> list[Any]:
        from apflow.core.execution.context import execution_context

        context = execution_context.get()
        data = getattr(context, "data", {})
        now = datetime.now(timezone.utc)
        if data.get("dispatch_task_id") == run_id:
            return [
                exists(
                    select(TaskDispatch.task_id).where(
                        TaskDispatch.task_id == run_id,
                        TaskDispatch.owner_token == data.get("dispatch_token"),
                        TaskDispatch.lease_expires_at > now,
                    )
                )
            ]
        if data.get("worker_root_id") == run_id:
            return [
                exists(
                    select(TaskLease.task_id).where(
                        TaskLease.task_id == run_id,
                        TaskLease.lease_token == data.get("worker_token"),
                        TaskLease.expires_at > now,
                    )
                )
            ]
        return []

    async def ensure_run(
        self, root_task_id: str, *, retry_of_run_id: str | None = None, commit: bool = True
    ) -> WorkflowRun:
        """Create a snapshot once; commit=False joins the caller's creation transaction."""
        await self.database.execute(
            update(TaskModel)
            .where(TaskModel.id == root_task_id)
            .values(id=TaskModel.id, updated_at=TaskModel.updated_at)
        )
        existing = (
            await self.database.execute(select(WorkflowRun).where(WorkflowRun.id == root_task_id))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        root = await self.repository.get_task_by_id(root_task_id)
        if root is None or root.parent_id is not None:
            raise ModuleError(code="INVALID_INPUT", message="A run requires an existing root task")
        tasks = await self._lock_run_tasks(root)
        run = new_run_record(root, tasks, retry_of_run_id=retry_of_run_id)
        if run.status == "accepted" and await self._has_invocations(tasks):
            run.status = "reconciliation_required"
            run.completed_at = datetime.now(timezone.utc)
        self.database.add(run)
        await self.database.flush()
        # Enrollment adds provenance to frozen rows without editing their business history.
        await self.database.execute(
            update(TaskModel)
            .where(TaskModel.id.in_([task.id for task in tasks]), TaskModel.run_id.is_(None))
            .values(run_id=run.id)
            .execution_options(synchronize_session="fetch")
        )
        if commit:
            await self.database.commit()
        return run

    async def _lock_run_tasks(self, root: TaskModel) -> list[TaskModel]:
        """Freeze descendants and include nodes added while acquiring their locks."""
        locked: set[str] = set()
        tasks = await self.repository.get_all_tasks_in_tree(root)
        while True:
            identifiers = {task.id for task in tasks}
            await self.repository.lock_tasks(sorted(identifiers - locked))
            locked.update(identifiers)
            for task in tasks:
                await self.database.refresh(task)
            tasks = await self.repository.get_all_tasks_in_tree(root)
            if all(task.id in locked for task in tasks):
                return tasks

    async def _has_invocations(self, tasks: list[TaskModel]) -> bool:
        identifier = (
            await self.database.execute(
                select(ExecutionAttempt.id)
                .where(ExecutionAttempt.task_id.in_([task.id for task in tasks]))
                .limit(1)
            )
        ).scalar_one_or_none()
        return identifier is not None

    async def start_run(self, root_task_id: str) -> WorkflowRun:
        """Atomically claim an accepted run; a terminal run requires a derived new run."""
        run = await self.ensure_run(root_task_id)
        if run.status != "accepted":
            raise ModuleError(
                code="RUN_NOT_EXECUTABLE",
                message="This run has already started or ended; create a new run to retry",
            )
        root = await self.repository.get_task_by_id(root_task_id)
        assert root is not None
        tasks = await self.repository.get_all_tasks_in_tree(root)
        if await self._has_invocations(tasks):
            await self.finish_run(run.id, "reconciliation_required")
            raise ModuleError(
                code="RUN_NOT_EXECUTABLE",
                message="This tree already has execution history; reconcile it before retrying",
            )
        snapshot = snapshot_run_definition(tasks)
        serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if hashlib.sha256(serialized.encode()).hexdigest() != run.definition_hash:
            raise ModuleError(
                code="RUN_DEFINITION_CHANGED", message="Accepted run definition changed"
            )
        changed = await self.database.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run.id,
                WorkflowRun.status == "accepted",
                *self._ownership_conditions(run.id),
            )
            .values(status="running", started_at=datetime.now(timezone.utc))
        )
        await self.database.commit()
        if not cast(CursorResult, changed).rowcount:
            raise ModuleError(
                code="RUN_NOT_EXECUTABLE",
                message=("This run has already started or ended; create a new run to retry"),
            )
        await self.database.refresh(run)
        return run

    async def finish_run(self, run_id: str, status: str) -> None:
        """Finalize only an active run; completed history is never reset."""
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError("A finished run requires a terminal status")
        await self.database.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.status.in_(["accepted", "running"]),
                *self._ownership_conditions(run_id),
            )
            .values(status=status, completed_at=datetime.now(timezone.utc))
        )
        await self.database.commit()

    async def finish_from_tasks(self, root_task_id: str) -> str:
        root = await self.repository.get_task_by_id(root_task_id)
        if root is None:
            raise ValueError("Run root no longer exists")
        tasks = await self.repository.get_all_tasks_in_tree(root)
        status = summarize_run(tasks)
        if status == "accepted":
            status = "reconciliation_required"
        await self.finish_run(root_task_id, status)
        return status
