"""Shared authorization for task modules and direct executor invocation."""

from __future__ import annotations

from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path
from typing import Any

from apcore.errors import ModuleError

from apflow.core.config_manager import get_config_manager
from apflow.core.execution.context import execution_context as execution_context


def task_owner(context: Any) -> str | None:
    """Return a verified caller identity, preserving trusted local library use."""
    identity = getattr(context, "identity", None)
    owner = getattr(identity, "id", None)
    if isinstance(owner, str) and owner:
        return owner
    if get_config_manager().get("security.require_identity", False):
        raise ModuleError(code="ACL_DENIED", message="A verified identity is required")
    return None


def is_administrator(context: Any) -> bool:
    """Only configured roles from a verified identity grant administrative access."""
    roles = getattr(getattr(context, "identity", None), "roles", ())
    allowed = get_config_manager().get("security.admin_roles", ["apflow.admin"])
    return bool(set(roles or ()) & set(allowed))


def authorize_capability(
    module_id: str, context: Any = None, *, inputs: dict[str, Any] | None = None
) -> None:
    """Apply deployment capability restrictions to both public execution paths."""
    task_owner(context)
    configuration = get_config_manager()
    acl_path = configuration.get("security.acl_path")
    if acl_path:
        acl = _load_acl(str(acl_path), Path(acl_path).stat().st_mtime_ns)
        from apcore import GovernanceProjection

        projection = GovernanceProjection.of(inputs) if inputs is not None else None
        caller_id = getattr(context, "caller_id", None)
        chain = getattr(context, "call_chain", [])
        if inputs is not None and chain and chain[-1] != module_id:
            caller_id = chain[-1]
        if not acl.check(caller_id, module_id, context, projection=projection):
            raise ModuleError(code="ACL_DENIED", message=f"Capability '{module_id}' is forbidden")
    allowed = configuration.get("security.allowed_modules", [])
    if allowed and not any(fnmatchcase(module_id, pattern) for pattern in allowed):
        raise ModuleError(code="ACL_DENIED", message=f"Capability '{module_id}' is disabled")
    role_rules = configuration.get("security.module_roles", {})
    roles = set(getattr(getattr(context, "identity", None), "roles", ()) or ())
    for pattern, required in role_rules.items():
        if fnmatchcase(module_id, pattern) and not roles.intersection(required):
            raise ModuleError(code="ACL_DENIED", message=f"Capability '{module_id}' is forbidden")


@lru_cache(maxsize=8)
def _load_acl(path: str, modified_at: int) -> Any:
    """Use apcore's policy contract, refreshing when deployment policy changes."""
    from apcore.acl import ACL

    return ACL.load(path)


def module_namespace(module: Any, action: str) -> str:
    """Use the registered capability namespace for receipts and policy checks."""
    identifier = getattr(module, "module_id", f"apflow.{action}")
    return identifier.removesuffix(f".{action}")


async def _protect_dispatched_definition(
    repository: Any, inputs: dict[str, Any], action: str
) -> None:
    if action not in {
        "task.update",
        "task.delete",
        "task.archive",
        "task.execute",
        "schedule.set",
        "schedule.complete",
        "schedule.trigger",
    }:
        return
    from sqlalchemy import select, update
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session
    from apflow.core.storage.sqlalchemy.models import TaskDispatch

    if not isinstance(getattr(repository.db, "_session", None), (Session, AsyncSession)):
        return
    task_id = inputs.get("task_id")
    if not task_id:
        return
    task = await repository.get_task_by_id(task_id)
    if task is None:
        return
    root = await repository.get_root_task(task)
    model = repository.task_model_class
    # Acceptance acquires this same write lock before checking task state. The
    # lock stays held until the public mutation commits or its session rolls back.
    if action != "schedule.trigger":
        await repository.db.execute(
            update(model)
            .where(model.id == root.id)
            .values(id=model.id, updated_at=model.updated_at)
        )
    active = await repository.db.execute(
        select(TaskDispatch.task_id).where(
            TaskDispatch.task_id == root.id,
            TaskDispatch.state.in_(
                ["accepted", "running", "cancel_requested", "reconciliation_required"]
            ),
        )
    )
    if active.scalar_one_or_none() is not None:
        raise ModuleError(
            code="TASK_DEFINITION_LOCKED",
            message="Submitted task definitions cannot be changed until execution or reconciliation finishes",
            user_fixable=True,
        )


async def authorize_task_inputs(
    repository: Any,
    inputs: dict[str, Any],
    context: Any,
    action: str,
    *,
    module_id: str | None = None,
) -> dict[str, Any]:
    """Scope queries and validate every referenced row before operating on a tree."""
    authorize_capability(module_id or f"apflow.{action}", context, inputs=inputs)
    owner = task_owner(context)
    scoped = dict(inputs)
    if owner is None or is_administrator(context):
        await _protect_dispatched_definition(repository, inputs, action)
        return scoped
    requested = inputs.get("user_id")
    if requested is not None and requested != owner:
        raise ModuleError(code="ACL_DENIED", message="Cannot select another task owner")
    scoped["user_id"] = owner
    requested_ids = inputs.get("task_ids")
    identifiers = list(requested_ids) if isinstance(requested_ids, list) else []
    for field in ("task_id", "parent_id"):
        if inputs.get(field):
            identifiers.append(inputs[field])
    visited: set[str] = set()
    while identifiers:
        identifier = identifiers.pop()
        if not isinstance(identifier, str) or identifier in visited:
            continue
        visited.add(identifier)
        if len(visited) > 10000:
            raise ModuleError(code="INVALID_INPUT", message="Task reference graph is too large")
        task = await repository.get_task_by_id(identifier)
        if task is None:
            continue
        if task.user_id != owner:
            raise ModuleError(code="ACL_DENIED", message="Task belongs to another identity")
        if task.parent_id:
            identifiers.append(task.parent_id)
        if action in {"task.copy", "task.link", "task.archive", "task.clone_mixed"}:
            from apflow.core.utils.credentials import reject_inline_credentials

            reject_inline_credentials(task.to_dict())
        if task.original_task_id:
            identifiers.append(task.original_task_id)
        identifiers.extend(
            dependency.get("id") if isinstance(dependency, dict) else dependency
            for dependency in task.dependencies or []
        )
        children = await repository.get_child_tasks_by_parent_id(identifier)
        identifiers.extend(child.id for child in children)
    await _protect_dispatched_definition(repository, inputs, action)
    return scoped
