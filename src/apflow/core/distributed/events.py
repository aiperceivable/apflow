"""Task event emission for distributed audit logging.

Writes lifecycle events (assigned, started, completed, failed, reassigned)
to the TaskEvent model for observability and debugging.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import sessionmaker

from apflow.core.storage.sqlalchemy.models import TaskEvent
from apflow.logger import get_logger

logger = get_logger(__name__)

VALID_EVENT_TYPES = frozenset(
    {
        "task_assigned",
        "task_started",
        "task_completed",
        "task_failed",
        "task_reassigned",
        "task_reconciliation_required",
    }
)


def emit_task_event(
    session_factory: sessionmaker,
    task_id: str,
    event_type: str,
    node_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Write a task lifecycle event to the database.

    Best-effort: logs and swallows exceptions so callers are never
    disrupted by event recording failures.
    """
    if not task_id:
        raise ValueError("task_id must not be empty")
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(
            f"event_type must be one of {sorted(VALID_EVENT_TYPES)}, got '{event_type}'"
        )

    try:
        with session_factory() as session:
            event = TaskEvent(
                task_id=task_id,
                event_type=event_type,
                node_id=node_id,
                details=details or {},
            )
            session.add(event)
            session.commit()
            logger.info(
                "Emitted event %s for task %s on node %s",
                event_type,
                task_id,
                node_id,
            )
    except Exception:
        logger.error(
            "Failed to emit event %s for task %s",
            event_type,
            task_id,
            exc_info=True,
        )
