"""Idempotent consumption facts, atomic projections, and delivery acknowledgements."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy_session_proxy import SqlalchemySessionProxy

from apflow.core.execution.result_protocol import normalize_executor_result
from apflow.core.storage.sqlalchemy.models import (
    BudgetReservation,
    ExecutionAttempt,
    TaskModel,
    UsageEvent,
)
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.governance.ledger import BudgetLedger, _validate_evidence

UsageKind = Literal["delta", "snapshot", "legacy"]


def normalized_usage(usage: dict[str, int]) -> dict[str, int]:
    """Normalize reported counters while preserving unknown versus explicit zero."""
    if not usage:
        raise ValueError("Usage must contain reported counters")
    result = normalize_executor_result({"result": None, "token_usage": usage}, "envelope")
    value = result.get("token_usage")
    if value is None:
        raise ValueError("Usage must contain valid counters")
    return dict(value)


class UsageRecorder:
    """Treat task totals as projections of immutable, uniquely identified events."""

    def __init__(self, session: Session | AsyncSession) -> None:
        self._session = session
        self._db = SqlalchemySessionProxy(session)

    async def record(
        self,
        invocation_id: str,
        event_id: str,
        usage: dict[str, int],
        *,
        kind: UsageKind = "delta",
        commit: bool = True,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Deduplicate facts; cumulative snapshots contribute only their new delta."""
        return await self._record(
            invocation_id,
            event_id,
            usage,
            kind=kind,
            commit=commit,
            evidence=evidence,
            allow_terminal=False,
        )

    async def _record(
        self,
        invocation_id: str,
        event_id: str,
        usage: dict[str, int],
        *,
        kind: UsageKind,
        commit: bool,
        evidence: dict[str, Any] | None,
        allow_terminal: bool,
    ) -> dict[str, Any]:
        """Keep deduplication and terminal authorization inside the invocation lock."""
        if kind not in {"delta", "snapshot", "legacy"}:
            raise ValueError("Invalid usage event kind")
        if evidence is not None:
            _validate_evidence(evidence)
        if not isinstance(event_id, str) or not event_id or len(event_id) > 255:
            raise ValueError("event_id must contain at most 255 characters")
        reported = normalized_usage(usage)
        digest = hashlib.sha256(
            json.dumps([invocation_id, kind, reported, evidence], sort_keys=True).encode()
        ).hexdigest()
        try:
            invocation = await self._lock_invocation(invocation_id)
            existing = (
                await self._db.execute(select(UsageEvent).where(UsageEvent.id == event_id))
            ).scalar_one_or_none()
            if existing is not None:
                if existing.digest != digest:
                    raise ValueError("Usage event ID was reported with different content")
                result = _event_dict(existing)
                if commit:
                    await self._db.commit()
                return result
            if invocation.status != "running" and not allow_terminal:
                raise ValueError("Terminal usage requires atomic reconciliation")
            event = await self._append(invocation, event_id, kind, digest, reported, evidence)
            result = _event_dict(event)
            if commit:
                await self._db.commit()
            else:
                await self._db.flush()
            return result
        except BaseException:
            await self._db.rollback()
            raise

    async def _lock_invocation(self, invocation_id: str) -> ExecutionAttempt:
        # Recovery locks the task before its invocations; accounting must use the same order.
        task_id = (
            select(ExecutionAttempt.task_id)
            .where(ExecutionAttempt.id == invocation_id)
            .scalar_subquery()
        )
        await self._db.execute(
            update(TaskModel)
            .where(TaskModel.id == task_id)
            .values(id=TaskModel.id, updated_at=TaskModel.updated_at)
        )
        # A write lock also serializes SQLite writers before snapshot delta calculation.
        await self._db.execute(
            update(ExecutionAttempt)
            .where(ExecutionAttempt.id == invocation_id)
            .values(id=ExecutionAttempt.id)
        )
        invocation = (
            await self._db.execute(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.id == invocation_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if invocation is None:
            raise KeyError("Execution invocation does not exist")
        return invocation

    async def _append(
        self,
        invocation: ExecutionAttempt,
        event_id: str,
        kind: UsageKind,
        digest: str,
        reported: dict[str, int],
        evidence: dict[str, Any] | None,
    ) -> UsageEvent:
        previous = await self.total(invocation.id)
        delta = (
            {name: reported[name] - previous[name] if name in reported else 0 for name in previous}
            if kind == "snapshot"
            else {name: reported.get(name, 0) for name in previous}
        )
        if any(value < 0 for value in delta.values()) and evidence is None:
            raise ValueError(
                "Cumulative usage cannot decrease; corrections require explicit evidence"
            )
        task = (
            await self._db.execute(select(TaskModel).where(TaskModel.id == invocation.task_id))
        ).scalar_one_or_none()
        event = UsageEvent(
            id=event_id,
            invocation_id=invocation.id,
            task_id=invocation.task_id,
            run_id=invocation.run_id,
            owner_id=task.user_id if task else None,
            kind=kind,
            digest=digest,
            reported_usage=reported,
            token_usage=delta,
            evidence=evidence,
        )
        self._db.add(event)
        await self._db.flush()
        if task is not None:
            await TaskRepository(self._session).increment_token_usage(
                task.id, delta["input"], delta["output"], delta["total"], commit=False
            )
        return event

    async def reconcile(
        self, invocation_id: str, event_id: str, usage: dict[str, int], evidence: dict[str, Any]
    ) -> dict[str, Any]:
        """Confirm terminal unknown consumption atomically, retaining original execution history."""
        _validate_evidence(evidence)
        try:
            invocation = await self._lock_invocation(invocation_id)
            if invocation.status == "running":
                raise ValueError("Running invocations cannot be reconciled")
            event = await self._record(
                invocation_id,
                event_id,
                usage,
                kind="snapshot",
                evidence=evidence,
                commit=False,
                allow_terminal=True,
            )
            reservation = (
                await self._db.execute(
                    select(BudgetReservation)
                    .where(BudgetReservation.id == invocation_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if reservation is not None:
                total = (await self.total(invocation_id))["total"]
                ledger = BudgetLedger(self._session)
                if reservation.state == "settled" and reservation.actual_tokens != total:
                    await ledger._correct_settlement(invocation_id, total, evidence)
                    await ledger._record_reconciliation(invocation_id, total, evidence)
                else:
                    await ledger.settle(invocation_id, total, reconciliation=evidence, commit=False)
            await self._db.commit()
            return event
        except BaseException:
            await self._db.rollback()
            raise

    async def preserve_legacy(self, task: TaskModel) -> None:
        """Seed pre-ledger counters once, without applying them to the task a second time."""
        if not task.token_usage or task.origin_type in {"copy", "link"}:
            return
        exists = (
            await self._db.execute(
                select(UsageEvent.id).where(UsageEvent.task_id == task.id).limit(1)
            )
        ).first()
        if exists:
            return
        event_id = "legacy:" + hashlib.sha256(task.id.encode()).hexdigest()
        usage = normalized_usage(task.token_usage)
        digest = hashlib.sha256(
            json.dumps([event_id, "legacy", usage, None], sort_keys=True).encode()
        ).hexdigest()
        self._db.add(
            UsageEvent(
                id=event_id,
                invocation_id=event_id,
                task_id=task.id,
                run_id=task.run_id,
                owner_id=task.user_id,
                kind="legacy",
                digest=digest,
                reported_usage=usage,
                token_usage=usage,
                created_at=task.completed_at or task.created_at or datetime.now(timezone.utc),
            )
        )
        await self._db.flush()

    async def total(self, invocation_id: str) -> dict[str, int]:
        values = (
            await self._db.execute(
                select(UsageEvent.token_usage).where(UsageEvent.invocation_id == invocation_id)
            )
        ).scalars()
        result = {"input": 0, "output": 0, "total": 0}
        for value in values:
            for name in result:
                result[name] += value.get(name, 0)
        return result

    async def list_events(
        self, owner_id: str | None, after_index: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Redeliver unacknowledged facts even below the cursor after late commits."""
        if isinstance(after_index, bool) or not isinstance(after_index, int) or after_index < 0:
            raise ValueError("after_index must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        rows = await self._db.execute(
            select(UsageEvent)
            .where(
                UsageEvent.owner_id == owner_id,
                or_(UsageEvent.index > after_index, UsageEvent.delivered_at.is_(None)),
                UsageEvent.kind != "legacy",
            )
            .order_by(UsageEvent.index)
            .limit(limit)
        )
        return [_event_dict(event) for event in rows.scalars()]

    async def acknowledge(self, event_id: str, owner_id: str | None) -> None:
        """Acknowledge delivery without modifying any consumption facts."""
        event = (
            await self._db.execute(
                select(UsageEvent).where(UsageEvent.id == event_id, UsageEvent.owner_id == owner_id)
            )
        ).scalar_one_or_none()
        if event is None:
            raise KeyError("Usage event does not exist for this owner")
        await self._db.execute(
            update(UsageEvent)
            .where(UsageEvent.id == event_id, UsageEvent.delivered_at.is_(None))
            .values(delivered_at=datetime.now(timezone.utc))
        )
        await self._db.commit()


def _event_dict(event: UsageEvent) -> dict[str, Any]:
    result = {column.name: getattr(event, column.name) for column in UsageEvent.__table__.columns}
    for name in ("created_at", "delivered_at"):
        value = result[name]
        result[name] = (
            value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat() if value else None
        )
    return result
