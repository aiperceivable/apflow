"""Storage invariants shared by supported ORM write paths."""

from typing import Any

from sqlalchemy import event, inspect, select, update
from sqlalchemy.orm import Session

_registered_models: set[type] = set()
MANAGED_DEFINITION_FIELDS = {
    "id",
    "max_attempts",
    "backoff_strategy",
    "backoff_base_seconds",
    "placement_constraints",
    "execution_identity",
    "name",
    "parent_id",
    "task_tree_id",
    "user_id",
    "dependencies",
    "inputs",
    "params",
    "schemas",
    "priority",
    "origin_type",
    "original_task_id",
    "source_task_id",
    "result_snapshot_hash",
    "run_id",
    "schedule_type",
    "schedule_expression",
    "schedule_enabled",
    "token_budget",
    "budget_scope_id",
    "cost_policy",
}


def definition_mutation_allowed(run_id: str | None, fields: set[str]) -> bool:
    """Only the active engine may materialize execution inputs and model selection."""
    changes = fields & MANAGED_DEFINITION_FIELDS
    if not changes:
        return True
    from apflow.core.execution.context import execution_context

    context = execution_context.get()
    data = getattr(context, "data", {})
    return changes <= {"inputs", "params"} and data.get("run_id") == run_id


def protect_archived_tasks(
    session: Session, task_class: Any, archive_origin: str, run_class: Any
) -> None:
    """Keep a frozen row immutable even when a supported writer uses ORM state."""
    from apflow.core.execution.errors import ValidationError

    _protect_run_history(session, run_class)
    _protect_archive_parents(session, task_class, archive_origin, run_class)

    for task in session.dirty.union(session.deleted):
        if not isinstance(task, task_class):
            continue
        state = inspect(task)
        changed = {
            column.key
            for column in state.mapper.column_attrs
            if state.attrs[column.key].history.has_changes()
        } - {"has_references", "updated_at"}
        if not changed and task not in session.deleted:
            continue
        identifier = str(state.identity[0]) if state.identity else str(task.id)
        stored_origin = _locked_origin(session, type(task), identifier)
        if stored_origin == archive_origin:
            raise ValidationError(f"Archived task {task.id} is immutable; copy it before editing")
        frozen, terminal_run, run_id = _frozen_history(session, type(task), identifier, run_class)
        billing_fields = {"token_usage", "actual_cost_usd", "estimated_cost_usd"}
        if frozen or terminal_run and (changed - billing_fields or task in session.deleted):
            raise ValidationError(f"Historical task {task.id} is immutable; derive a new run")
        if run_id is not None and (
            task in session.deleted or not definition_mutation_allowed(run_id, changed)
        ):
            raise ValidationError(f"Run task {task.id} definition is immutable")


def _protect_run_history(session: Session, run_class: Any) -> None:
    """A caller cannot rewrite the accepted definition or reopen terminal history."""
    from apflow.core.execution.errors import ValidationError

    immutable = {
        "id",
        "root_task_id",
        "user_id",
        "definition_snapshot",
        "definition_hash",
        "retry_of_run_id",
        "created_at",
    }
    for run in session.dirty.union(session.deleted):
        if not isinstance(run, run_class):
            continue
        state = inspect(run)
        changed = {
            column.key
            for column in run.__table__.columns
            if state.attrs[column.key].history.has_changes()
        }
        identifier = state.identity[0] if state.identity else run.id
        status = (
            session.connection()
            .execute(select(run_class.status).where(run_class.id == identifier).with_for_update())
            .scalar_one_or_none()
        )
        terminal = status in {"completed", "failed", "cancelled", "reconciliation_required"}
        if run in session.deleted or changed & immutable or terminal and changed:
            raise ValidationError(f"Run {identifier} history is immutable")


def _protect_archive_parents(
    session: Session, task_class: Any, archive_origin: str, run_class: Any
) -> None:
    """Adding, removing, or moving a node cannot change a frozen parent's structure."""
    from apflow.core.execution.errors import ValidationError

    changed = session.new.union(session.dirty).union(session.deleted)
    pending = {str(task.id): task for task in changed if isinstance(task, task_class)}
    for task in changed:
        if not isinstance(task, task_class):
            continue
        history = inspect(task).attrs.parent_id.history
        if task not in session.new and task not in session.deleted and not history.has_changes():
            continue
        parent_ids = {str(parent) for parent in history.deleted if parent is not None}
        if task.parent_id is not None:
            parent_ids.add(str(task.parent_id))
        for parent_id in sorted(parent_ids):
            parent = pending.get(parent_id)
            if parent is not None and parent.origin_type == archive_origin:
                raise ValidationError(f"Archived task {parent_id} cannot change children")
            if _locked_origin(session, type(task), parent_id) == archive_origin:
                raise ValidationError(f"Archived task {parent_id} cannot change children")
            frozen, terminal_run, run_id = _frozen_history(
                session, type(task), parent_id, run_class
            )
            if frozen or terminal_run or run_id is not None:
                raise ValidationError(f"Historical task {parent_id} cannot change children")


def _frozen_history(
    session: Session, task_class: Any, task_id: str, run_class: Any
) -> tuple[bool, bool, str | None]:
    """Read persisted markers so changing a marker cannot bypass immutability."""

    row = (
        session.connection()
        .execute(
            select(task_class.result_snapshot_hash, run_class.status, run_class.id)
            .outerjoin(run_class, run_class.id == task_class.run_id)
            .where(task_class.id == task_id)
        )
        .first()
    )
    if row is None:
        return False, False, None
    return (
        row[0] is not None,
        row[1]
        in {
            "completed",
            "failed",
            "cancelled",
            "reconciliation_required",
        },
        row[2],
    )


def _locked_origin(session: Session, task_class: Any, task_id: str) -> str | None:
    connection = session.connection()
    if connection.dialect.name == "sqlite":
        connection.execute(
            update(task_class)
            .where(task_class.id == task_id)
            .values(updated_at=task_class.updated_at)
        )
    return connection.execute(
        select(task_class.origin_type).where(task_class.id == task_id).with_for_update()
    ).scalar_one_or_none()


def register_storage_invariants(task_class: Any, archive_origin: str, run_class: Any) -> None:
    """Install one listener for normal and AsyncSession-backed ORM writes."""
    if task_class in _registered_models:
        return

    def protect(session: Session, flush_context: Any, instances: Any) -> None:
        protect_archived_tasks(session, task_class, archive_origin, run_class)

    event.listen(Session, "before_flush", protect)
    _registered_models.add(task_class)
