"""Storage invariants shared by supported ORM write paths."""

from typing import Any

from sqlalchemy import event, inspect, select, update
from sqlalchemy.orm import Session

_registered_models: set[type] = set()


def protect_archived_tasks(session: Session, task_class: Any, archive_origin: str) -> None:
    """Keep a frozen row immutable even when a supported writer uses ORM state."""
    from apflow.core.execution.errors import ValidationError

    _protect_archive_parents(session, task_class, archive_origin)

    for task in session.dirty.union(session.deleted):
        if not isinstance(task, task_class):
            continue
        state = inspect(task)
        changed = {
            column.key
            for column in task.__table__.columns
            if state.attrs[column.key].history.has_changes()
        } - {"has_references", "updated_at"}
        if not changed and task not in session.deleted:
            continue
        stored_origin = _locked_origin(session, type(task), str(task.id))
        if stored_origin == archive_origin:
            raise ValidationError(f"Archived task {task.id} is immutable; copy it before editing")


def _protect_archive_parents(session: Session, task_class: Any, archive_origin: str) -> None:
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


def register_storage_invariants(task_class: Any, archive_origin: str) -> None:
    """Install one listener for normal and AsyncSession-backed ORM writes."""
    if task_class in _registered_models:
        return

    def protect(session: Session, flush_context: Any, instances: Any) -> None:
        protect_archived_tasks(session, task_class, archive_origin)

    event.listen(Session, "before_flush", protect)
    _registered_models.add(task_class)
