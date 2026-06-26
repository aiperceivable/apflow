"""
Checkpoint manager for saving and restoring task execution state.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Union

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from apflow.logger import get_logger

logger = get_logger(__name__)


class CheckpointManager:
    """Manages checkpoint persistence for task execution state.

    Checkpoints are stored in the `task_checkpoints` table and referenced
    from the task's `checkpoint_at` and `resume_from` fields.

    Supports both synchronous ``Session`` and asynchronous ``AsyncSession`` so it
    can run on the same live session the executor uses. All methods are async;
    the sync-session path performs the same work without awaiting the driver.
    """

    def __init__(self, db: Union[Session, AsyncSession]) -> None:
        if db is None:
            raise TypeError("db session must not be None")
        self._db = db

    async def _commit(self) -> None:
        db = self._db
        if isinstance(db, AsyncSession):
            await db.commit()
        else:
            db.commit()

    async def save_checkpoint(
        self,
        task_id: str,
        data: dict[str, Any],
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

        from apflow.core.storage.sqlalchemy.models import TaskCheckpointModel, TaskModel

        checkpoint_id = str(uuid.uuid4())
        checkpoint = TaskCheckpointModel(
            id=checkpoint_id,
            task_id=task_id,
            checkpoint_data=serialized,
            step_name=step_name,
            created_at=datetime.now(timezone.utc),
        )
        db = self._db
        db.add(checkpoint)

        # Update task's checkpoint reference via the ORM (no raw SQL table names).
        if isinstance(db, AsyncSession):
            task = (
                await db.execute(select(TaskModel).where(TaskModel.id == task_id))
            ).scalar_one_or_none()
        else:
            task = db.query(TaskModel).filter(TaskModel.id == task_id).first()
        if task is not None:
            task.checkpoint_at = datetime.now(timezone.utc)
            task.resume_from = checkpoint_id

        await self._commit()
        logger.debug(f"Saved checkpoint {checkpoint_id} for task {task_id}")
        return checkpoint_id

    async def load_checkpoint(self, task_id: str) -> Optional[dict[str, Any]]:
        """Load the latest checkpoint for a task.

        Returns:
            Checkpoint data dict, or None if no checkpoint exists.
        """
        if not task_id:
            raise ValueError("task_id must be non-empty")

        from apflow.core.storage.sqlalchemy.models import TaskCheckpointModel

        db = self._db
        stmt = (
            select(TaskCheckpointModel)
            .where(TaskCheckpointModel.task_id == task_id)
            .order_by(TaskCheckpointModel.created_at.desc())
        )
        if isinstance(db, AsyncSession):
            result = (await db.execute(stmt)).scalars().first()
        else:
            result = (
                db.query(TaskCheckpointModel)
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

        db = self._db
        if isinstance(db, AsyncSession):
            from sqlalchemy import delete as sa_delete

            result = await db.execute(
                sa_delete(TaskCheckpointModel).where(TaskCheckpointModel.task_id == task_id)
            )
            count = result.rowcount or 0
        else:
            count = (
                db.query(TaskCheckpointModel)
                .filter(TaskCheckpointModel.task_id == task_id)
                .delete()
            )
        await self._commit()
        logger.debug(f"Deleted {count} checkpoints for task {task_id}")
        return count
