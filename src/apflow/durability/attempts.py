"""Queryable invocation history shared by local and distributed execution."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import hashlib
import json

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy_session_proxy import SqlalchemySessionProxy

from apflow.core.execution.error_info import ErrorInfo, validate_error_info
from apflow.core.execution.result_protocol import normalize_executor_result
from apflow.core.storage.sqlalchemy.models import (
    BudgetReservation,
    ExecutionAttempt,
    TaskModel,
    UsageEvent,
)
from apflow.core.utils.credentials import redact_public_data
from apflow.governance.ledger import BudgetLedger
from apflow.governance.usage import UsageRecorder, normalized_usage


class AttemptRecorder:
    """Write required execution diagnostics on the engine's live storage session."""

    def __init__(self, session: Session | AsyncSession) -> None:
        self._session = session
        self._db = SqlalchemySessionProxy(session)

    async def start(self, task_id: str, attempt_id: int, attempt_number: int) -> str:
        if not task_id or not isinstance(task_id, str) or len(task_id) > 255:
            raise ValueError("task_id must be non-empty and at most 255 characters")
        if min(attempt_id, attempt_number) < 0:
            raise ValueError("Attempt counters must be non-negative")
        record_id = uuid4().hex
        await self._db.execute(
            update(TaskModel).where(TaskModel.id == task_id).values(updated_at=TaskModel.updated_at)
        )
        task = (
            await self._db.execute(
                select(TaskModel)
                .where(TaskModel.id == task_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if task is not None:
            await UsageRecorder(self._session).preserve_legacy(task)
        self._db.add(
            ExecutionAttempt(
                id=record_id,
                task_id=task_id,
                run_id=task.run_id if task else None,
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
        info = (
            dict(validate_error_info(redact_public_data(error_info)))
            if error_info is not None
            else None
        )
        usage = None
        if token_usage:
            usage = normalize_executor_result(
                {"result": None, "token_usage": token_usage}, "envelope"
            ).get("token_usage")
        try:
            recorder = UsageRecorder(self._session)
            invocation = await recorder._lock_invocation(record_id)
            if invocation.status != "running":
                await self._record_late_usage(invocation, usage)
                return
            if usage:
                await recorder.record(
                    record_id, record_id + ":final", usage, kind="snapshot", commit=False
                )
            total = await recorder.total(record_id)
            await self._settle_reservation(record_id, total["total"] if usage is not None else None)
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
        except BaseException:
            await self._db.rollback()
            raise

    async def _settle_reservation(self, record_id: str, actual_tokens: int | None) -> None:
        reservation = (
            await self._db.execute(
                select(BudgetReservation.id).where(BudgetReservation.id == record_id)
            )
        ).scalar_one_or_none()
        if reservation is not None:
            await BudgetLedger(self._session).settle(record_id, actual_tokens, commit=False)

    async def _record_late_usage(
        self, invocation: ExecutionAttempt, usage: dict[str, int] | None
    ) -> None:
        if usage and (invocation.status == "unknown" or invocation.token_usage is None):
            event_id = invocation.id + ":late-final"
            previous = (
                await self._db.execute(select(UsageEvent).where(UsageEvent.id == event_id))
            ).scalar_one_or_none()
            if previous is not None:
                if previous.reported_usage != usage:
                    raise ValueError("Invocation was reported with different final usage")
                await self._db.commit()
                return
            recorder = UsageRecorder(self._session)
            total = await recorder.total(invocation.id)
            prior_evidence = (
                await self._db.execute(
                    select(UsageEvent.evidence).where(
                        UsageEvent.invocation_id == invocation.id,
                        UsageEvent.id != event_id,
                    )
                )
            ).scalars()
            if any(value is not None for value in prior_evidence):
                if any(usage[name] != total[name] for name in usage):
                    raise ValueError(
                        "Late executor usage conflicts with authoritative reconciliation"
                    )
                await self._db.commit()
                return
            if any(usage[name] < total[name] for name in usage):
                raise ValueError("Late executor usage cannot decrease confirmed consumption")
            evidence = {
                "evidence_ref": "executor:" + invocation.id + ":final",
                "actor": None,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
            await recorder.reconcile(invocation.id, event_id, usage, evidence)
        else:
            if usage and invocation.token_usage != usage:
                raise ValueError("Invocation was reported with different final usage")
            await self._db.commit()

    async def record_partial(self, task_id: str, generation: int, usage: dict[str, int]) -> None:
        """Cancellation reports are cumulative snapshots, including repeated reports."""
        if not usage:
            return
        result = await self._db.execute(
            select(ExecutionAttempt)
            .where(
                ExecutionAttempt.task_id == task_id,
                ExecutionAttempt.attempt_id == generation,
            )
            .order_by(ExecutionAttempt.started_at.desc(), ExecutionAttempt.id)
            .limit(1)
        )
        invocation = result.scalar_one_or_none()
        if invocation is None:
            record_id = hashlib.sha256(
                f"legacy-partial:{task_id}:{generation}".encode()
            ).hexdigest()
            task = (
                await self._db.execute(select(TaskModel).where(TaskModel.id == task_id))
            ).scalar_one_or_none()
            if task is not None:
                await UsageRecorder(self._session).preserve_legacy(task)
            invocation = ExecutionAttempt(
                id=record_id,
                task_id=task_id,
                run_id=task.run_id if task else None,
                attempt_id=generation,
                attempt_number=0,
                status="running",
                started_at=datetime.now(timezone.utc),
            )
            self._db.add(invocation)
            await self._db.flush()
        await self._record_partial_snapshot(invocation.id, usage)

    async def _record_partial_snapshot(self, invocation_id: str, usage: dict[str, int]) -> None:
        reported = normalized_usage(usage)
        digest = hashlib.sha256(json.dumps(reported, sort_keys=True).encode()).hexdigest()
        event_id = invocation_id + ":partial:" + digest
        recorder = UsageRecorder(self._session)
        try:
            invocation = await recorder._lock_invocation(invocation_id)
            existing = (
                await self._db.execute(select(UsageEvent.id).where(UsageEvent.id == event_id))
            ).scalar_one_or_none()
            if existing is None and invocation.status != "running":
                total = await recorder.total(invocation_id)
                if any(reported[name] > total[name] for name in reported):
                    raise ValueError("Terminal invocations require evidenced usage reconciliation")
                await self._db.commit()
                return
            await recorder.record(invocation_id, event_id, reported, kind="snapshot")
        except BaseException:
            await self._db.rollback()
            raise

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
