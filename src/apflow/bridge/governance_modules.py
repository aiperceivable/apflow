"""Caller-scoped shared budget configuration, inspection, and reconciliation."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from apcore import ModuleAnnotations
from apcore.errors import ModuleError
from sqlalchemy import select

from apflow.bridge.access import authorize_capability, is_administrator, task_owner
from apflow.bridge.errors import invalid_input_error
from apflow.governance.ledger import BudgetLedger, scope_budget_key
from apflow.core.storage.sqlalchemy.models import ExecutionAttempt, SubmissionReceipt


def _integer_input(value: Any, name: str) -> int:
    """Normalize exact protobuf numbers without accepting fractions or lost precision."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer() and abs(value) <= 2**53 - 1:
        return int(value)
    raise invalid_input_error(f"{name} must be an exact integer")


class BudgetScopeModule:
    """Manage one shared limit without allowing an update to erase accumulated usage."""

    description = "Configure or inspect a shared token budget in the verified caller's scope."
    annotations = ModuleAnnotations()
    input_schema = {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "minLength": 1},
            "owner_id": {"type": "string", "minLength": 1},
            "limit_tokens": {"type": "integer", "minimum": 0},
        },
        "required": ["scope"],
    }
    output_schema = {"type": "object"}

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        authorize_capability(
            getattr(self, "module_id", "apflow.budget.scope"), context, inputs=inputs
        )
        owner = task_owner(context)
        if inputs.get("owner_id") is not None and inputs["owner_id"] != owner:
            if not is_administrator(context):
                raise ModuleError(
                    code="ACL_DENIED",
                    message="Only an administrator may select another budget owner",
                )
            owner = inputs["owner_id"]
        key = scope_budget_key(owner, inputs["scope"])
        ledger = BudgetLedger(self._repo.db._session)
        if "limit_tokens" in inputs:
            try:
                snapshot = await ledger.configure_scope(
                    key, _integer_input(inputs["limit_tokens"], "limit_tokens")
                )
            except ValueError as error:
                raise ModuleError(
                    code="INVALID_INPUT",
                    message="Budget scope limit is invalid or conflicts with its existing limit",
                ) from error
        else:
            try:
                snapshot = await ledger.get_scope(key)
            except KeyError as error:
                raise ModuleError(
                    code="MODULE_NOT_FOUND", message="Budget scope does not exist"
                ) from error
        return {
            **asdict(snapshot),
            "remaining_tokens": snapshot.remaining_tokens,
            "reservations": await ledger.list_reservations(key),
        }


class BudgetReconcileModule:
    """Settle an unknown liability only with explicit administrative evidence."""

    description = "Reconcile a budget reservation against a provider usage receipt."
    annotations = ModuleAnnotations(destructive=True, requires_approval=True)
    input_schema = {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "minLength": 1},
            "owner_id": {"type": "string", "minLength": 1},
            "reservation_id": {"type": "string", "minLength": 1},
            "actual_tokens": {"type": "integer", "minimum": 0},
            "evidence_ref": {"type": "string", "minLength": 1},
        },
        "required": ["scope", "reservation_id", "actual_tokens", "evidence_ref"],
    }
    output_schema = {"type": "object"}

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        authorize_capability(
            getattr(self, "module_id", "apflow.budget.reconcile"), context, inputs=inputs
        )
        if task_owner(context) is not None and not is_administrator(context):
            raise ModuleError(
                code="ACL_DENIED", message="Usage reconciliation requires an administrator"
            )
        inputs = {
            **inputs,
            "actual_tokens": _integer_input(inputs["actual_tokens"], "actual_tokens"),
        }
        key = scope_budget_key(inputs.get("owner_id", task_owner(context)), inputs["scope"])
        ledger = BudgetLedger(self._repo.db._session)
        reservation = await ledger.get_reservation(key, inputs["reservation_id"])
        if reservation is None:
            raise ModuleError(code="ACL_DENIED", message="Reservation is outside this budget scope")
        try:
            await self._settle_confirmed(ledger, key, inputs, context)
        except ValueError as error:
            raise ModuleError(
                code="INVALID_INPUT",
                message="Invalid or conflicting usage reconciliation",
                user_fixable=True,
            ) from error
        result = await self._repo.db.execute(
            select(SubmissionReceipt).where(
                SubmissionReceipt.scope == "budget-reconciliation:" + key,
                SubmissionReceipt.key == inputs["reservation_id"],
            )
        )
        audit = result.scalar_one()
        return {
            "reservation_id": inputs["reservation_id"],
            "state": "settled",
            "evidence_ref": audit.receipt["evidence_ref"],
        }

    async def _settle_confirmed(
        self, ledger: BudgetLedger, key: str, inputs: dict[str, Any], context: Any
    ) -> None:
        """Publish newly confirmed invocation usage in the settlement transaction."""
        invocation = (
            await self._repo.db.execute(
                select(ExecutionAttempt).where(ExecutionAttempt.id == inputs["reservation_id"])
            )
        ).scalar_one_or_none()
        if invocation is not None:
            from apflow.governance.usage import UsageRecorder

            recorder = UsageRecorder(self._repo.db._session)
            await recorder._lock_invocation(invocation.id)
            evidence = await self._evidence(key, inputs, context)
            await recorder.reconcile(
                invocation.id,
                "reconcile:" + invocation.id,
                {"total": inputs["actual_tokens"]},
                evidence,
            )
        else:
            evidence = await self._evidence(key, inputs, context)
            await ledger.settle(
                inputs["reservation_id"], inputs["actual_tokens"], reconciliation=evidence
            )

    async def _evidence(self, key: str, inputs: dict[str, Any], context: Any) -> dict[str, Any]:
        """Reuse the first evidence timestamp so repeated delivery has the same digest."""
        audit = (
            await self._repo.db.execute(
                select(SubmissionReceipt).where(
                    SubmissionReceipt.scope == "budget-reconciliation:" + key,
                    SubmissionReceipt.key == inputs["reservation_id"],
                )
            )
        ).scalar_one_or_none()
        if audit is not None:
            return {name: audit.receipt[name] for name in ("evidence_ref", "actor", "observed_at")}
        return {
            "evidence_ref": inputs["evidence_ref"],
            "actor": task_owner(context),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }


class UsageEventsModule:
    """Expose immutable usage facts to an owner-scoped billing consumer."""

    description = "Read a cursor page of immutable usage events for billing delivery."
    annotations = ModuleAnnotations(readonly=True)
    input_schema = {
        "type": "object",
        "properties": {
            "after_index": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        },
    }
    output_schema = {"type": "object"}

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        from apflow.governance.usage import UsageRecorder

        authorize_capability(
            getattr(self, "module_id", "apflow.usage.events"), context, inputs=inputs
        )
        cursor = _integer_input(inputs.get("after_index", 0), "after_index")
        try:
            events = await UsageRecorder(self._repo.db._session).list_events(
                task_owner(context), cursor, _integer_input(inputs.get("limit", 100), "limit")
            )
        except ValueError as error:
            raise ModuleError(code="INVALID_INPUT", message=str(error)) from error
        return {
            "events": events,
            "next_index": max(cursor, events[-1]["index"]) if events else cursor,
        }


class UsageAcknowledgeModule:
    """Mark delivery only after the billing consumer has persisted an event."""

    description = "Acknowledge durable delivery of one owned usage event."
    annotations = ModuleAnnotations(idempotent=True)
    input_schema = {
        "type": "object",
        "properties": {"event_id": {"type": "string", "minLength": 1, "maxLength": 255}},
        "required": ["event_id"],
    }
    output_schema = {"type": "object"}

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        from apflow.governance.usage import UsageRecorder

        authorize_capability(getattr(self, "module_id", "apflow.usage.ack"), context, inputs=inputs)
        try:
            await UsageRecorder(self._repo.db._session).acknowledge(
                inputs["event_id"], task_owner(context)
            )
        except KeyError as error:
            raise ModuleError(
                code="MODULE_NOT_FOUND", message="Usage event does not exist"
            ) from error
        return {"event_id": inputs["event_id"], "acknowledged": True}
