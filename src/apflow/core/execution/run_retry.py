"""Derive a fresh run while retaining immutable, unbilled completed results."""

from __future__ import annotations

import copy
from typing import Any
from uuid import uuid4

from apcore.errors import ModuleError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from apflow.core.execution.result_protocol import result_snapshot_hash
from apflow.core.execution.runs import RunService
from apflow.core.execution.submission import _read_receipt, submission_scope
from apflow.core.execution.task_creator import TaskCreator
from apflow.core.storage.sqlalchemy.models import (
    SubmissionReceipt,
    TaskModel,
    TaskOriginType,
    WorkflowRun,
)
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


def _dependency_ids(task: TaskModel) -> set[str]:
    return {
        str(dependency["id"] if isinstance(dependency, dict) else dependency)
        for dependency in task.dependencies or []
    }


def _select_nodes(tasks: list[TaskModel], selected: list[str] | None) -> set[str]:
    """Use DAG closure for execution and retain ancestors only for organization."""
    by_id = {task.id: task for task in tasks}
    required = set(by_id) if selected is None else set(selected)
    if not required or not required.issubset(by_id):
        raise ModuleError(code="INVALID_INPUT", message="Selected tasks must belong to the run")
    if selected is not None:
        dependents: dict[str, set[str]] = {}
        for task in tasks:
            for dependency in _dependency_ids(task):
                dependents.setdefault(dependency, set()).add(task.id)
        pending = list(required)
        while pending:
            additions = dependents.get(pending.pop(), set()) - required
            required.update(additions)
            pending.extend(additions)
    pending = list(required)
    while pending:
        identifier = pending.pop()
        dependencies = _dependency_ids(by_id[identifier])
        if not dependencies.issubset(by_id):
            raise ModuleError(
                code="INVALID_INPUT", message="Run has dependencies outside its snapshot"
            )
        additions = dependencies - required
        required.update(additions)
        pending.extend(additions)
    for identifier in list(required):
        parent_id = by_id[identifier].parent_id
        visited: set[str] = set()
        while parent_id:
            if parent_id in visited or parent_id not in by_id:
                raise ModuleError(code="INVALID_INPUT", message="Invalid task structure")
            visited.add(parent_id)
            required.add(parent_id)
            parent_id = by_id[parent_id].parent_id
    return required


def _remap_dependencies(task: TaskModel, identifiers: dict[str, str]) -> list[Any]:
    dependencies = copy.deepcopy(task.dependencies or [])
    for index, dependency in enumerate(dependencies):
        original_id = str(dependency["id"] if isinstance(dependency, dict) else dependency)
        if original_id not in identifiers:
            raise ModuleError(
                code="INVALID_INPUT", message="Retry is missing a required dependency"
            )
        if isinstance(dependency, dict):
            dependency["id"] = identifiers[original_id]
        else:
            dependencies[index] = identifiers[original_id]
    return dependencies


async def _clone_nodes(
    repository: TaskRepository, tasks: list[TaskModel], required: set[str], execute: set[str]
) -> tuple[str, dict[str, str]]:
    identifiers = {task.id: str(uuid4()) for task in tasks if task.id in required}
    root = next(task for task in tasks if task.parent_id is None)
    parents = {task.parent_id for task in tasks if task.id in required}
    for source in tasks:
        if source.id not in required:
            continue
        fields = TaskCreator._fresh_execution_fields()
        fields.update(
            id=identifiers[source.id],
            source_task_id=source.id,
            original_task_id=source.original_task_id or source.id,
            run_id=identifiers[root.id],
            task_tree_id=identifiers[root.id],
            parent_id=identifiers.get(source.parent_id) if source.parent_id else None,
            origin_type=TaskOriginType.copy,
            dependencies=_remap_dependencies(source, identifiers),
            has_children=source.id in parents,
            schedule_enabled=False,
            schedule_type=None,
            schedule_expression=None,
            next_run_at=None,
            last_run_at=None,
            run_count=0,
        )
        if source.status == "completed" and source.id not in execute:
            effective = await repository.resolve_effective_task(source)
            fields.update(
                origin_type=TaskOriginType.link,
                status="completed",
                progress=1.0,
                result=copy.deepcopy(effective.result),
                result_snapshot_hash=result_snapshot_hash(effective.result),
            )
        repository.db.add(source.copy(fields))
    await repository.db.flush()
    return identifiers[root.id], identifiers


async def retry_run(
    repository: TaskRepository,
    run_id: str,
    key: str,
    owner: str | None,
    *,
    selected_task_ids: list[str] | None = None,
    namespace: str = "apflow",
    replay_safe: bool = False,
    evidence_ref: str | None = None,
    administrator: bool = False,
) -> dict[str, Any]:
    """Commit the new tree, Run and idempotent receipt in one transaction."""
    request = _retry_request(run_id, key, selected_task_ids, replay_safe, evidence_ref)
    digest = result_snapshot_hash(request)
    scope = submission_scope(owner, f"{namespace}:run.retry")
    existing = await _read_receipt(repository.db, scope, key, digest)
    if existing is not None:
        return existing
    try:
        receipt = await _derive_run(
            repository,
            run_id,
            selected_task_ids,
            replay_safe,
            evidence_ref,
            None if administrator else owner,
        )
        repository.db.add(SubmissionReceipt(scope=scope, key=key, digest=digest, receipt=receipt))
        await repository.db.commit()
        return receipt
    except IntegrityError:
        await repository.db.rollback()
        existing = await _read_receipt(repository.db, scope, key, digest)
        if existing is None:
            raise
        return existing
    except Exception:
        await repository.db.rollback()
        raise


def _retry_request(
    run_id: str,
    key: str,
    selected_task_ids: list[str] | None,
    replay_safe: bool,
    evidence_ref: str | None,
) -> list[Any]:
    if not isinstance(key, str) or not key.strip() or len(key) > 255:
        raise ModuleError(
            code="INVALID_INPUT", message="idempotency_key must contain 1 to 255 characters"
        )
    if selected_task_ids is not None and (
        not isinstance(selected_task_ids, list)
        or not selected_task_ids
        or any(
            not isinstance(identifier, str) or not identifier for identifier in selected_task_ids
        )
    ):
        raise ModuleError(
            code="INVALID_INPUT", message="task_ids must contain nonempty task identifiers"
        )
    return [
        run_id,
        sorted(set(selected_task_ids)) if selected_task_ids is not None else None,
        replay_safe,
        evidence_ref,
    ]


async def _derive_run(
    repository: TaskRepository,
    run_id: str,
    selected: list[str] | None,
    replay_safe: bool,
    evidence_ref: str | None,
    owner: str | None,
) -> dict[str, Any]:

    source = await _source_run(repository, run_id, owner)
    root = await repository.get_task_by_id(source.root_task_id)
    if root is None:
        raise ModuleError(code="MODULE_NOT_FOUND", message="Run root not found")
    tasks = await repository.get_all_tasks_in_tree(root)
    tasks = _restore_definition(tasks, source.definition_snapshot, source.definition_hash)
    from apflow.core.validator.dependency_validator import detect_circular_dependencies

    try:
        detect_circular_dependencies(tasks)
    except ValueError as error:
        raise ModuleError(
            code="INVALID_INPUT", message="Run dependencies contain a cycle"
        ) from error
    required = _select_nodes(tasks, selected)
    _validate_replay(source.status, tasks, required, replay_safe, evidence_ref)
    execute = _execution_nodes(tasks, required, selected)
    new_root, identifiers = await _clone_nodes(repository, tasks, required, execute)
    run = await RunService(repository.db._session).ensure_run(
        new_root, retry_of_run_id=source.id, commit=False
    )
    return {
        "run_id": run.id,
        "root_task_id": new_root,
        "state": run.status,
        "retry_of_run_id": source.id,
        "task_mapping": identifiers,
        "execution_task_ids": [identifiers[identifier] for identifier in sorted(execute)],
        "reused_task_ids": [identifiers[identifier] for identifier in sorted(required - execute)],
        "evidence_ref": evidence_ref,
    }


def _validate_replay(
    status: str,
    tasks: list[TaskModel],
    required: set[str],
    replay_safe: bool,
    evidence_ref: str | None,
) -> None:
    uncertain = status == "reconciliation_required" or any(
        task.id in required
        and (
            task.status == "in_progress"
            or isinstance(task.error_info, dict)
            and task.error_info.get("delivery_state") == "unknown"
        )
        for task in tasks
    )
    if uncertain and (not replay_safe or not evidence_ref):
        raise ModuleError(
            code="RECONCILIATION_REQUIRED",
            message="Unknown outcomes require explicit replay authorization and evidence",
        )


def _execution_nodes(
    tasks: list[TaskModel], required: set[str], selected: list[str] | None
) -> set[str]:
    """A new upstream result invalidates previously completed downstream results."""
    execute = {task.id for task in tasks if task.id in required and task.status != "completed"}
    execute.update(selected or [])
    dependents: dict[str, set[str]] = {}
    for task in tasks:
        if task.id in required:
            for dependency in _dependency_ids(task):
                dependents.setdefault(dependency, set()).add(task.id)
    pending = list(execute)
    while pending:
        additions = dependents.get(pending.pop(), set()) - execute
        execute.update(additions)
        pending.extend(additions)
    return execute


async def _source_run(repository: TaskRepository, run_id: str, owner: str | None) -> WorkflowRun:
    result = await repository.db.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))
    source = result.scalar_one_or_none()
    if source is None:
        root = await repository.get_task_by_id(run_id)
        if root is None or root.parent_id is not None:
            raise ModuleError(code="MODULE_NOT_FOUND", message="Run not found")
        source = await RunService(repository.db._session).ensure_run(run_id, commit=False)
    if owner is not None and source.user_id != owner:
        raise ModuleError(code="ACL_DENIED", message="Run belongs to another identity")
    if source.status not in {"failed", "cancelled", "reconciliation_required"}:
        raise ModuleError(
            code="INVALID_INPUT", message="Retry requires a terminal unsuccessful run"
        )
    return source


def _restore_definition(
    tasks: list[TaskModel],
    snapshot: dict[str, Any],
    expected_hash: str,
) -> list[TaskModel]:
    """Use accepted configuration rather than runtime-mutated dependency inputs."""
    if result_snapshot_hash(snapshot) != expected_hash:
        raise ModuleError(code="INVALID_INPUT", message="Run definition snapshot is invalid")
    if _has_redactions(snapshot):
        raise ModuleError(
            code="INVALID_INPUT",
            message="Retry configuration contains redacted credentials; use credential_ref in a fresh definition",
        )
    definitions = {row["id"]: row for row in snapshot.get("tasks", [])}
    if set(definitions) != {task.id for task in tasks}:
        raise ModuleError(
            code="INVALID_INPUT", message="Run nodes differ from the accepted snapshot"
        )
    return [task.copy(copy.deepcopy(definitions[task.id])) for task in tasks]


def _has_redactions(value: Any) -> bool:
    """A sanitized snapshot cannot supply an original inline credential."""
    if isinstance(value, dict):
        return any(_has_redactions(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_redactions(item) for item in value)
    return isinstance(value, str) and "***REDACTED***" in value
