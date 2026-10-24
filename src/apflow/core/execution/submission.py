"""Transactional task creation with durable, caller-scoped submission receipts."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from apcore.errors import ModuleError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from apflow.core.storage.sqlalchemy.models import SubmissionReceipt


def submission_scope(owner: str | None, operation: str) -> str:
    """Keep receipt namespaces bounded without trusting request ownership fields."""
    return hashlib.sha256(json.dumps([owner, operation]).encode()).hexdigest()


async def _read_receipt(database: Any, scope: str, key: str, digest: str) -> dict[str, Any] | None:
    result = await database.execute(
        select(SubmissionReceipt).where(
            SubmissionReceipt.scope == scope, SubmissionReceipt.key == key
        )
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        return None
    if existing.digest != digest:
        raise ModuleError(
            code="IDEMPOTENCY_CONFLICT",
            message="Submission key already identifies different content",
        )
    return copy.deepcopy(existing.receipt)


def _creation_receipt(trees: list[Any], shape: str) -> dict[str, Any]:
    root = trees[0].task
    if shape == "task":
        return {
            "id": root.id,
            "name": root.name,
            "status": root.status,
            "created_at": root.created_at.isoformat() if root.created_at else None,
        }
    identifiers = [task.id for tree in trees for task in tree.to_list()]
    return {"root_task_id": root.id, "task_count": len(identifiers), "task_ids": identifiers}


async def create_with_receipt(
    creator: Any,
    repository: Any,
    tasks: list[dict[str, Any]],
    key: Any,
    owner: str | None,
    shape: str,
    namespace: str = "apflow",
) -> dict[str, Any]:
    """Persist the tree and its receipt in one transaction, including racing submits."""
    if not isinstance(key, str) or not key.strip() or len(key) > 255:
        raise ModuleError(
            code="INVALID_INPUT", message="idempotency_key must contain 1 to 255 characters"
        )
    scope = submission_scope(owner, f"{namespace}:{shape}")
    try:
        # Authentication snapshots are service metadata, not request content.
        # Token refreshes must not turn a retry into a conflicting submission.
        request_tasks = [
            {key: value for key, value in task.items() if key != "execution_identity"}
            for task in tasks
        ]
        canonical = json.dumps(
            request_tasks, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as error:
        raise ModuleError(
            code="INVALID_INPUT", message="Task definitions must be JSON values"
        ) from error
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    database = repository.db
    existing = await _read_receipt(database, scope, key, digest)
    if existing is not None:
        return existing
    try:
        trees = await creator.create_task_trees_from_array(copy.deepcopy(tasks), commit=False)
        receipt = _creation_receipt(trees, shape)
        database.add(SubmissionReceipt(scope=scope, key=key, digest=digest, receipt=receipt))
        await database.commit()
        return receipt
    except IntegrityError:
        await database.rollback()
        existing = await _read_receipt(database, scope, key, digest)
        if existing is None:
            raise
        return existing
    except Exception:
        await database.rollback()
        raise
