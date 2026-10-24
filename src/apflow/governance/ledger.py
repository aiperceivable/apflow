"""Durable token reservations, shared across tasks and worker processes."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy_session_proxy import SqlalchemySessionProxy

from apflow.core.execution.errors import BusinessError
from apflow.core.storage.sqlalchemy.models import (
    BudgetAccount,
    BudgetReservation,
    SubmissionReceipt,
)


class BudgetExceededError(BusinessError):
    """A shared account has insufficient unencumbered tokens for this invocation."""


@dataclass(frozen=True)
class BudgetSnapshot:
    """Unknown tokens are held liabilities, not a measurement of actual usage."""

    scope_id: str
    limit_tokens: int
    spent_tokens: int
    reserved_tokens: int
    unknown_tokens: int

    @property
    def remaining_tokens(self) -> int:
        return max(
            0, self.limit_tokens - self.spent_tokens - self.reserved_tokens - self.unknown_tokens
        )


def scope_budget_key(owner: str | None, scope: str) -> str:
    """Namespace a public scope name with the trusted authenticated owner."""
    _identifier(scope, "scope")
    if owner is not None:
        _identifier(owner, "owner")
    encoded = json.dumps([owner, scope], separators=(",", ":")).encode()
    return "scope-" + hashlib.sha256(encoded).hexdigest()


class BudgetLedger:
    """Each mutation commits before remote work; no automatic expiry frees liabilities."""

    def __init__(self, session: Session | AsyncSession) -> None:
        self._db = SqlalchemySessionProxy(session)

    async def configure_scope(self, scope_id: str, limit_tokens: int) -> BudgetSnapshot:
        """Create or verify a balance; this operation never resets existing spend."""
        _identifier(scope_id, "scope_id")
        _tokens(limit_tokens)
        try:
            await self._db.execute(
                text(
                    "INSERT INTO apflow_budget_accounts "
                    "(scope_id, limit_tokens, spent_tokens, reserved_tokens, unknown_tokens) "
                    "VALUES (:scope, :limit, 0, 0, 0) ON CONFLICT (scope_id) DO NOTHING"
                ),
                {"scope": scope_id, "limit": limit_tokens},
            )
            snapshot = await self.get_scope(scope_id)
            if snapshot.limit_tokens != limit_tokens:
                raise ValueError("Budget scope already exists with a different limit")
            await self._db.commit()
            return snapshot
        except Exception:
            await self._db.rollback()
            raise

    async def get_scope(self, scope_id: str) -> BudgetSnapshot:
        _identifier(scope_id, "scope_id")
        result = await self._db.execute(
            select(
                BudgetAccount.scope_id,
                BudgetAccount.limit_tokens,
                BudgetAccount.spent_tokens,
                BudgetAccount.reserved_tokens,
                BudgetAccount.unknown_tokens,
            ).where(BudgetAccount.scope_id == scope_id)
        )
        row = result.one_or_none()
        if row is None:
            raise KeyError("Budget scope does not exist")
        return BudgetSnapshot(*row)

    async def list_reservations(self, scope_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """List attempts within an already authorized scope for status and reconciliation."""
        _identifier(scope_id, "scope_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        rows = await self._db.execute(
            select(
                BudgetReservation.id,
                BudgetReservation.task_id,
                BudgetReservation.state,
                BudgetReservation.reserved_tokens,
                BudgetReservation.actual_tokens,
            )
            .where(BudgetReservation.scope_id == scope_id)
            .order_by(BudgetReservation.created_at.desc(), BudgetReservation.id)
            .limit(limit)
        )
        return [dict(row) for row in rows.mappings()]

    async def get_reservation(self, scope_id: str, reservation_id: str) -> dict[str, Any] | None:
        """Perform an exact authorized lookup, independent of the history page size."""
        _identifier(scope_id, "scope_id")
        _identifier(reservation_id, "reservation_id")
        result = await self._db.execute(
            select(BudgetReservation.__table__).where(
                BudgetReservation.scope_id == scope_id, BudgetReservation.id == reservation_id
            )
        )
        row = result.mappings().one_or_none()
        return dict(row) if row is not None else None

    async def reserve(self, scope_id: str, task_id: str, reservation_id: str, tokens: int) -> str:
        """Atomically reserve shared headroom and create one idempotent attempt record."""
        for name, value in (
            ("scope_id", scope_id),
            ("task_id", task_id),
            ("reservation_id", reservation_id),
        ):
            _identifier(value, name)
        _tokens(tokens)
        try:
            inserted = await self._insert_reservation(scope_id, task_id, reservation_id, tokens)
            if not inserted:
                await self._verify_reservation(scope_id, task_id, reservation_id, tokens)
            else:
                await self._hold_tokens(scope_id, tokens)
            await self._db.commit()
            return reservation_id
        except Exception:
            await self._db.rollback()
            raise

    async def _insert_reservation(
        self, scope_id: str, task_id: str, reservation_id: str, tokens: int
    ) -> bool:
        result = await self._db.execute(
            text(
                "INSERT INTO apflow_budget_reservations "
                "(id, scope_id, task_id, state, reserved_tokens) "
                "VALUES (:id, :scope, :task, 'reserved', :tokens) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": reservation_id, "scope": scope_id, "task": task_id, "tokens": tokens},
        )
        return isinstance(result, CursorResult) and result.rowcount == 1

    async def _verify_reservation(
        self, scope_id: str, task_id: str, reservation_id: str, tokens: int
    ) -> None:
        row = (
            await self._db.execute(
                select(
                    BudgetReservation.scope_id,
                    BudgetReservation.task_id,
                    BudgetReservation.reserved_tokens,
                ).where(BudgetReservation.id == reservation_id)
            )
        ).one()
        if tuple(row) != (scope_id, task_id, tokens):
            raise ValueError("Reservation key is bound to a different invocation")

    async def _hold_tokens(self, scope_id: str, tokens: int) -> None:
        account = BudgetAccount
        available = (
            account.limit_tokens
            - account.spent_tokens
            - account.reserved_tokens
            - account.unknown_tokens
        )
        result = await self._db.execute(
            update(account)
            .where(account.scope_id == scope_id, available >= tokens)
            .values(reserved_tokens=account.reserved_tokens + tokens)
            .execution_options(synchronize_session=False)
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise BudgetExceededError("Shared token budget is missing or has insufficient capacity")

    async def settle(
        self,
        reservation_id: str,
        actual_tokens: int | None,
        *,
        reconciliation: dict[str, Any] | None = None,
    ) -> None:
        """Settle exactly once, or keep an unknown charge held for later reconciliation."""
        _identifier(reservation_id, "reservation_id")
        if actual_tokens is not None:
            _tokens(actual_tokens)
        try:
            sources = ("reserved",) if actual_tokens is None else ("reserved", "unknown")
            for source in sources:
                if await self._transition_reservation(reservation_id, source, actual_tokens):
                    if reconciliation is not None:
                        await self._record_reconciliation(
                            reservation_id, actual_tokens, reconciliation
                        )
                    await self._db.commit()
                    return
            await self._verify_settlement(reservation_id, actual_tokens)
            if reconciliation is not None:
                await self._record_reconciliation(reservation_id, actual_tokens, reconciliation)
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise

    async def _record_reconciliation(
        self, reservation_id: str, actual_tokens: int | None, evidence: dict[str, Any]
    ) -> None:
        """Commit the first operator evidence in the same transaction as its settlement."""
        if actual_tokens is None:
            raise ValueError("Reconciliation must report actual token usage")
        _validate_evidence(evidence)
        scope_id = (
            await self._db.execute(
                select(BudgetReservation.scope_id).where(BudgetReservation.id == reservation_id)
            )
        ).scalar_one()
        audit = {"reservation_id": reservation_id, "actual_tokens": actual_tokens, **evidence}
        digest = hashlib.sha256(
            json.dumps(audit, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        insert = (
            postgresql_insert if self._db.get_bind().dialect.name == "postgresql" else sqlite_insert
        )
        await self._db.execute(
            insert(SubmissionReceipt)
            .values(
                scope="budget-reconciliation:" + scope_id,
                key=reservation_id,
                digest=digest,
                receipt=audit,
            )
            .on_conflict_do_nothing(index_elements=["scope", "key"])
        )

    async def _transition_reservation(
        self, reservation_id: str, source: str, actual_tokens: int | None
    ) -> bool:
        result = await self._db.execute(
            update(BudgetReservation)
            .where(BudgetReservation.id == reservation_id, BudgetReservation.state == source)
            .values(
                state="unknown" if actual_tokens is None else "settled", actual_tokens=actual_tokens
            )
            .returning(BudgetReservation.scope_id, BudgetReservation.reserved_tokens)
            .execution_options(synchronize_session=False)
        )
        row = result.one_or_none()
        if row is None:
            return False
        scope_id, held = row
        changes: dict[str, Any] = {
            "reserved_tokens" if source == "reserved" else "unknown_tokens": (
                BudgetAccount.reserved_tokens
                if source == "reserved"
                else BudgetAccount.unknown_tokens
            )
            - held
        }
        if actual_tokens is None:
            changes["unknown_tokens"] = BudgetAccount.unknown_tokens + held
        else:
            changes["spent_tokens"] = BudgetAccount.spent_tokens + actual_tokens
        await self._db.execute(
            update(BudgetAccount)
            .where(BudgetAccount.scope_id == scope_id)
            .values(**changes)
            .execution_options(synchronize_session=False)
        )
        return True

    async def _verify_settlement(self, reservation_id: str, actual_tokens: int | None) -> None:
        row = (
            await self._db.execute(
                select(BudgetReservation.state, BudgetReservation.actual_tokens).where(
                    BudgetReservation.id == reservation_id
                )
            )
        ).one_or_none()
        if row is None:
            raise KeyError("Budget reservation does not exist")
        if actual_tokens is not None and (
            row.state != "settled" or row.actual_tokens != actual_tokens
        ):
            raise ValueError("Budget reservation was already settled with different usage")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise ValueError(f"{name} must be a non-empty string of at most 255 characters")


def _tokens(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Token amounts must be non-negative integers")


def _validate_evidence(evidence: dict[str, Any]) -> None:
    if set(evidence) != {"evidence_ref", "actor", "observed_at"}:
        raise ValueError("Reconciliation requires evidence_ref, actor, and observed_at")
    reference = evidence["evidence_ref"]
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 2048:
        raise ValueError("evidence_ref must contain at most 2048 characters")
    if evidence["actor"] is not None:
        _identifier(evidence["actor"], "actor")
    _identifier(evidence["observed_at"], "observed_at")
