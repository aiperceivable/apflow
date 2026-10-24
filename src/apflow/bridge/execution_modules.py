"""Persistent submission and explicit reconciliation operations."""

from __future__ import annotations

from typing import Any
from typing import cast
from datetime import datetime, timezone
from uuid import uuid4
import hashlib
import json

from apcore import ModuleAnnotations
from apcore.errors import ModuleError
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from apflow.bridge.access import (
    authorize_task_inputs,
    is_administrator,
    module_namespace,
    task_owner,
)
from apflow.core.execution.submission import submission_scope
from apflow.core.storage.sqlalchemy.models import SubmissionReceipt, TaskDispatch


def _evidence_reference(value: Any) -> str:
    """Accept only bounded, printable receipt references for the audit trail."""
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ModuleError(
            code="INVALID_INPUT", message="evidence_ref must contain 1 to 1024 characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ModuleError(
            code="INVALID_INPUT", message="evidence_ref must not contain control characters"
        )
    from apflow.core.utils.credentials import redact_public_data

    if redact_public_data(value) != value:
        raise ModuleError(code="INVALID_INPUT", message="evidence_ref must not contain credentials")
    return value.strip()


class TaskSubmitModule:
    """Accept a root durably; subscriptions and queries never start execution."""

    description = "Submit a pending root for durable background execution."
    annotations = ModuleAnnotations(idempotent=True)
    input_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string", "minLength": 1}},
        "required": ["task_id"],
    }
    output_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string"}, "state": {"type": "string"}},
        "required": ["task_id", "state"],
    }

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        inputs = await authorize_task_inputs(
            self._repo, inputs, context, "task.submit", module_id=getattr(self, "module_id", None)
        )
        from apflow.core.execution.dispatch import accept_task

        return await accept_task(self._repo.db._session, inputs.get("task_id", ""), context)


class TaskReceiptModule:
    """Look up a previous create receipt after losing a submission response."""

    description = "Look up an idempotent create receipt within the verified caller's scope."
    annotations = ModuleAnnotations(readonly=True, idempotent=True)
    input_schema = {
        "type": "object",
        "properties": {
            "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 255},
            "shape": {"type": "string", "enum": ["task", "tree"], "default": "task"},
        },
        "required": ["idempotency_key"],
    }
    output_schema = {"type": "object"}

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        await authorize_task_inputs(
            self._repo, inputs, context, "task.receipt", module_id=getattr(self, "module_id", None)
        )
        shape = inputs.get("shape", "task")
        if shape not in ("task", "tree"):
            raise ModuleError(code="INVALID_INPUT", message="Unknown receipt shape")
        scope = submission_scope(
            task_owner(context), f"{module_namespace(self, 'task.receipt')}:{shape}"
        )
        result = await self._repo.db.execute(
            select(SubmissionReceipt).where(
                SubmissionReceipt.scope == scope,
                SubmissionReceipt.key == inputs.get("idempotency_key"),
            )
        )
        receipt = result.scalar_one_or_none()
        if receipt is None:
            raise ModuleError(code="MODULE_NOT_FOUND", message="Submission receipt not found")
        return dict(receipt.receipt)


class TaskReconcileModule:
    """Allow an operator to resume only after checking an uncertain remote outcome."""

    description = "Resume an interrupted run after an operator confirms remote replay is safe."
    annotations = ModuleAnnotations(destructive=True, requires_approval=True)
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "replay_safe": {"type": "boolean"},
            "evidence_ref": {"type": "string", "minLength": 1},
        },
        "required": ["task_id", "replay_safe", "evidence_ref"],
    }
    output_schema = {"type": "object"}

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        await authorize_task_inputs(
            self._repo,
            inputs,
            context,
            "task.reconcile",
            module_id=getattr(self, "module_id", None),
        )
        if task_owner(context) is not None and not is_administrator(context):
            raise ModuleError(code="ACL_DENIED", message="Reconciliation requires an administrator")
        if inputs.get("replay_safe") is not True:
            raise ModuleError(
                code="INVALID_INPUT", message="Confirm replay safety and supply receipt evidence"
            )
        evidence = _evidence_reference(inputs.get("evidence_ref"))
        root = await self._repo.get_task_by_id(inputs.get("task_id", ""))
        if root is None:
            raise ModuleError(code="MODULE_NOT_FOUND", message="Reconciliation task does not exist")
        if root.parent_id is not None or root.origin_type in ("archive", "link"):
            raise ModuleError(
                code="INVALID_INPUT", message="Reconciliation requires an executable root task"
            )
        changed = await self._repo.db.execute(
            update(TaskDispatch)
            .where(
                TaskDispatch.task_id == inputs["task_id"],
                TaskDispatch.state == "reconciliation_required",
                TaskDispatch.owner_token.is_(None),
            )
            .values(state="accepted")
        )
        if not cast(CursorResult, changed).rowcount:
            raise ModuleError(code="INVALID_INPUT", message="Run is not awaiting reconciliation")
        rows = await self._repo.get_all_tasks_in_tree(root)
        for task in rows:
            if task.status in ("in_progress", "failed"):
                task.status = "pending"
                task.cancel_requested_at = None
        audit = {
            "task_id": inputs["task_id"],
            "evidence_ref": evidence,
            "actor": task_owner(context),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._repo.db.add(
            SubmissionReceipt(
                scope=f"task-reconciliation:{inputs['task_id']}",
                key=uuid4().hex,
                digest=hashlib.sha256(json.dumps(audit, sort_keys=True).encode()).hexdigest(),
                receipt=audit,
            )
        )
        await self._repo.db.commit()
        return {
            "task_id": inputs["task_id"],
            "state": "accepted",
            "evidence_ref": evidence,
        }
