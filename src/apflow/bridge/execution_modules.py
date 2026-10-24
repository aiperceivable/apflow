"""Persistent submission and explicit reconciliation operations."""

from __future__ import annotations

from typing import Any

from apcore import ModuleAnnotations
from apcore.errors import ModuleError
from sqlalchemy import select

from apflow.bridge.access import (
    authorize_task_inputs,
    is_administrator,
    module_namespace,
    task_owner,
)
from apflow.core.execution.submission import submission_scope
from apflow.core.storage.sqlalchemy.models import SubmissionReceipt


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
    """Reject the obsolete operation that reset historical task states in place."""

    description = "Historical runs are immutable; use run.retry to derive a new run."
    annotations = ModuleAnnotations(readonly=True)
    input_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
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
        if await self._repo.get_task_by_id(inputs.get("task_id", "")) is None:
            raise ModuleError(code="MODULE_NOT_FOUND", message="Reconciliation task does not exist")
        raise ModuleError(
            code="INVALID_INPUT",
            message=("Runs cannot be reset in place; use run.retry with an idempotency key"),
        )


class RunGetModule:
    """Return the immutable run definition and current lifecycle state."""

    description = "Get a run, its definition snapshot and direct retry lineage."
    annotations = ModuleAnnotations(readonly=True, idempotent=True)
    input_schema = {
        "type": "object",
        "properties": {"run_id": {"type": "string", "minLength": 1}},
        "required": ["run_id"],
    }
    output_schema = {"type": "object"}

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        await authorize_task_inputs(
            self._repo, inputs, context, "run.get", module_id=getattr(self, "module_id", None)
        )
        run = await _get_run(self._repo, inputs.get("run_id", ""), context)
        from apflow.core.utils.credentials import redact_public_data

        return redact_public_data(run.to_dict())


async def _get_run(repository: Any, run_id: str, context: Any) -> Any:
    from apflow.core.storage.sqlalchemy.models import WorkflowRun

    result = await repository.db.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))
    run = result.scalar_one_or_none()
    if run is None:
        raise ModuleError(code="MODULE_NOT_FOUND", message="Run not found")
    owner = task_owner(context)
    if owner is not None and run.user_id != owner and not is_administrator(context):
        raise ModuleError(code="ACL_DENIED", message="Run belongs to another identity")
    return run


class RunRetryModule:
    """Create a new run while reusing confirmed results from a terminal run."""

    description = (
        "Derive a new run from a failed, cancelled or uncertain run. Selected tasks include "
        "affected downstream nodes and required dependencies; unchanged completed results are frozen references."
    )
    annotations = ModuleAnnotations(idempotent=True)
    input_schema = {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "minLength": 1},
            "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 255},
            "task_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "replay_safe": {"type": "boolean", "default": False},
            "evidence_ref": {"type": "string", "minLength": 1, "maxLength": 1024},
        },
        "required": ["run_id", "idempotency_key"],
    }
    output_schema = {"type": "object"}

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        await authorize_task_inputs(
            self._repo, inputs, context, "run.retry", module_id=getattr(self, "module_id", None)
        )
        replay_safe = inputs.get("replay_safe") is True
        if replay_safe and task_owner(context) is not None and not is_administrator(context):
            raise ModuleError(
                code="ACL_DENIED", message="Replay authorization requires an administrator"
            )
        evidence = _evidence_reference(inputs["evidence_ref"]) if "evidence_ref" in inputs else None
        from apflow.core.execution.run_retry import retry_run

        return await retry_run(
            self._repo,
            inputs.get("run_id", ""),
            inputs.get("idempotency_key", ""),
            task_owner(context),
            selected_task_ids=inputs.get("task_ids"),
            namespace=module_namespace(self, "run.retry"),
            replay_safe=replay_safe,
            evidence_ref=evidence,
            administrator=is_administrator(context),
        )
