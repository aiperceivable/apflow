"""Queryable invocation history shared by local and distributed execution."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy_session_proxy import SqlalchemySessionProxy

from apflow.core.execution.error_info import ErrorInfo, validate_error_info
from apflow.core.execution.result_protocol import normalize_executor_result
from apflow.core.storage.sqlalchemy.models import ExecutionAttempt


class AttemptRecorder:
    """Write required execution diagnostics on the engine's live storage session."""

    def __init__(self, session: Session | AsyncSession) -> None:
        self._db = SqlalchemySessionProxy(session)

    async def start(self, task_id: str, attempt_id: int, attempt_number: int) -> str:
        if not task_id or not isinstance(task_id, str) or len(task_id) > 255:
            raise ValueError("task_id must be non-empty and at most 255 characters")
        if min(attempt_id, attempt_number) < 0:
            raise ValueError("Attempt counters must be non-negative")
        record_id = uuid4().hex
        self._db.add(
            ExecutionAttempt(
                id=record_id,
                task_id=task_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                status="running",
                started_at=datetime.now(timezone.utc),
            )
        )
        await self._commit()
        return record_id

    async def finish(
        self,
        record_id: str,
        status: str,
        *,
        error_info: ErrorInfo | None = None,
        token_usage: dict[str, int] | None = None,
    ) -> None:
        """A completed attempt is immutable; late writers cannot replace its evidence."""
        if status not in {"completed", "failed", "cancelled", "unknown"}:
            raise ValueError("Invalid execution attempt terminal status")
        info = dict(validate_error_info(error_info)) if error_info is not None else None
        usage = None
        if token_usage is not None:
            usage = normalize_executor_result(
                {"result": None, "token_usage": token_usage}, "envelope"
            ).get("token_usage")
        await self._db.execute(
            update(ExecutionAttempt)
            .where(ExecutionAttempt.id == record_id, ExecutionAttempt.status == "running")
            .values(
                status=status,
                error_info=info,
                token_usage=usage,
                completed_at=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )
        await self._commit()

    async def schedule_retry(self, record_id: str, retry_at: datetime) -> None:
        if retry_at.tzinfo is None:
            raise ValueError("retry_at must include a timezone")
        await self._db.execute(
            update(ExecutionAttempt)
            .where(ExecutionAttempt.id == record_id, ExecutionAttempt.status == "failed")
            .values(next_retry_at=retry_at)
            .execution_options(synchronize_session=False)
        )
        await self._commit()

    async def _commit(self) -> None:
        try:
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise


async def list_execution_attempts(
    task_id: str, session: Session | AsyncSession, limit: int = 100
) -> list[dict[str, Any]]:
    """Read a bounded history after the service has authorized access to the task."""
    if not task_id or not isinstance(task_id, str) or len(task_id) > 255:
        raise ValueError("task_id must be non-empty and at most 255 characters")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000")
    result = await SqlalchemySessionProxy(session).execute(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.task_id == task_id)
        .order_by(ExecutionAttempt.started_at.desc(), ExecutionAttempt.id)
        .limit(limit)
    )
    return [_attempt_dict(row) for row in result.scalars()]


def _attempt_dict(attempt: ExecutionAttempt) -> dict[str, Any]:
    result: dict[str, Any] = {
        column.name: getattr(attempt, column.name) for column in ExecutionAttempt.__table__.columns
    }
    for name in ("started_at", "completed_at", "next_retry_at"):
        value = result[name]
        result[name] = (
            value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat() if value else None
        )
    return result
