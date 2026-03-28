"""
Checkpoint manager for saving and restoring task execution state.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from apflow.logger import get_logger

logger = get_logger(__name__)


class CheckpointManager:
    """Manages checkpoint persistence for task execution state.

    Checkpoints are stored in the `task_checkpoints` table and referenced
    from the task's `checkpoint_at` and `resume_from` fields.
    """

    def __init__(self, db: Session) -> None:
        if db is None:
            raise TypeError("db session must not be None")
        self._db = db

    async def save_checkpoint(
        self,
        task_id: str,
        data: Dict[str, Any],
        step_name: Optional[str] = None,
    ) -> str:
        """Save a checkpoint for a task.

        Args:
            task_id: Task ID (non-empty).
            data: JSON-serializable checkpoint data.
            step_name: Optional name for the checkpoint step.

        Returns:
            Checkpoint ID (UUID string).
        """
        if not task_id:
            raise ValueError("task_id must be non-empty")
        if not isinstance(data, dict):
            raise TypeError(f"data must be a dict, got {type(data)}")

        try:
            serialized = json.dumps(data)
        except (TypeError, ValueError) as e:
            raise ValueError(f"Checkpoint data is not JSON-serializable: {e}") from e

        from apflow.core.storage.sqlalchemy.models import TaskCheckpointModel, TASK_TABLE_NAME

        checkpoint_id = str(uuid.uuid4())
        checkpoint = TaskCheckpointModel(
            id=checkpoint_id,
            task_id=task_id,
            checkpoint_data=serialized,
            step_name=step_name,
            created_at=datetime.now(timezone.utc),
        )
        self._db.add(checkpoint)

        # Update task's checkpoint reference
        from sqlalchemy import text

        self._db.execute(
            text(
                f"UPDATE {TASK_TABLE_NAME} SET checkpoint_at = :ts, resume_from = :cp_id "
                f"WHERE id = :tid"
            ),
            {"ts": datetime.now(timezone.utc), "cp_id": checkpoint_id, "tid": task_id},
        )

        self._db.commit()
        logger.debug(f"Saved checkpoint {checkpoint_id} for task {task_id}")
        return checkpoint_id

    async def load_checkpoint(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Load the latest checkpoint for a task.

        Returns:
            Checkpoint data dict, or None if no checkpoint exists.
        """
        if not task_id:
            raise ValueError("task_id must be non-empty")

        from apflow.core.storage.sqlalchemy.models import TaskCheckpointModel

        result = (
            self._db.query(TaskCheckpointModel)
            .filter(TaskCheckpointModel.task_id == task_id)
            .order_by(TaskCheckpointModel.created_at.desc())
            .first()
        )

        if result is None:
            return None

        return json.loads(result.checkpoint_data)

    async def delete_checkpoints(self, task_id: str) -> int:
        """Delete all checkpoints for a task.

        Returns:
            Number of checkpoints deleted.
        """
        if not task_id:
            raise ValueError("task_id must be non-empty")

        from apflow.core.storage.sqlalchemy.models import TaskCheckpointModel

        count = (
            self._db.query(TaskCheckpointModel)
            .filter(TaskCheckpointModel.task_id == task_id)
            .delete()
        )
        self._db.commit()
        logger.debug(f"Deleted {count} checkpoints for task {task_id}")
        return count
