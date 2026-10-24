"""Retry runs preserve historical executions and reuse frozen, unbilled results."""

from decimal import Decimal
import json
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from apflow.core.execution.task_creator import TaskCreator
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.core.execution.runs import RunService
from apflow.bridge.execution_modules import RunRetryModule, RunGetModule
from apcore import Context, Identity
from apcore.errors import ModuleError


async def _failed_run(session: Session) -> tuple[TaskRepository, list[str]]:
    repository = TaskRepository(session)
    root = await repository.create_task(name="Root", status="completed", user_id="alice")
    first = await repository.create_task(
        name="A",
        parent_id=root.id,
        task_tree_id=root.id,
        status="completed",
        result={"value": [7]},
        token_usage={"total_tokens": 100},
        actual_cost_usd=Decimal("1"),
        user_id="alice",
    )
    failed = await repository.create_task(
        name="B",
        parent_id=root.id,
        task_tree_id=root.id,
        status="failed",
        dependencies=[first.id],
        token_usage={"total_tokens": 20},
        user_id="alice",
    )
    last = await repository.create_task(
        name="C",
        parent_id=root.id,
        task_tree_id=root.id,
        status="pending",
        dependencies=[{"id": failed.id, "required": True}],
        user_id="alice",
    )
    await RunService(session).ensure_run(root.id)
    return repository, [root.id, first.id, failed.id, last.id]


async def _task_rows(repository: TaskRepository, identifiers: list[str]) -> list[dict[str, Any]]:
    rows = []
    for identifier in identifiers:
        row = await repository.get_task_by_id(identifier)
        assert row is not None
        rows.append(row.to_dict())
    return rows


@pytest.mark.asyncio
async def test_retry_derives_new_run_with_frozen_unbilled_results(sync_db_session: Session) -> None:
    repository, identifiers = await _failed_run(sync_db_session)
    context = Context.create(identity=Identity(id="alice"))
    before = await _task_rows(repository, identifiers)
    request = {"run_id": identifiers[0], "idempotency_key": "retry-1"}
    module = RunRetryModule(repository)
    receipt = await module.execute(request, context)
    assert await module.execute(request, context) == receipt
    assert receipt["run_id"] != identifiers[0]
    assert receipt["retry_of_run_id"] == identifiers[0]
    mapping = receipt["task_mapping"]
    frozen = await repository.get_task_by_id(mapping[identifiers[1]])
    assert frozen is not None and frozen.result_snapshot_hash is not None
    assert frozen.status == "completed" and frozen.token_usage is None
    assert frozen.actual_cost_usd is None
    assert (await repository.resolve_effective_task(frozen)).result == {"value": [7]}
    failed = await repository.get_task_by_id(mapping[identifiers[2]])
    last = await repository.get_task_by_id(mapping[identifiers[3]])
    assert failed is not None and last is not None
    assert failed.status == last.status == "pending"
    assert failed.token_usage is None and failed.completed_at is None
    assert failed.dependencies == [frozen.id]
    assert last.dependencies == [{"id": failed.id, "required": True}]
    after = await _task_rows(repository, identifiers)
    assert before == after
    run = await RunGetModule(repository).execute({"run_id": receipt["run_id"]}, context)
    assert run["retry_of_run_id"] == identifiers[0]
    json.dumps(run)
    with pytest.raises(ModuleError) as conflict:
        await module.execute({**request, "task_ids": [identifiers[2]]}, context)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_retry_and_read_deny_another_owner(sync_db_session: Session) -> None:
    repository, identifiers = await _failed_run(sync_db_session)
    context = Context.create(identity=Identity(id="bob"))
    with pytest.raises(ModuleError) as denied:
        await RunRetryModule(repository).execute(
            {"run_id": identifiers[0], "idempotency_key": "retry-1"}, context
        )
    assert denied.value.code == "ACL_DENIED"
    with pytest.raises(ModuleError):
        await RunGetModule(repository).execute({"run_id": identifiers[0]}, context)


@pytest.mark.asyncio
async def test_terminal_run_and_reused_snapshot_are_immutable(sync_db_session: Session) -> None:
    from apflow.core.execution.errors import ValidationError

    repository, identifiers = await _failed_run(sync_db_session)
    with pytest.raises(ValidationError, match="Historical"):
        await repository.update_task(identifiers[2], status="pending")
    receipt = await RunRetryModule(repository).execute(
        {"run_id": identifiers[0], "idempotency_key": "retry-1"}
    )
    frozen = await repository.get_task_by_id(receipt["task_mapping"][identifiers[1]])
    assert frozen is not None
    frozen.result = {"tampered": True}
    with pytest.raises(ValidationError):
        sync_db_session.commit()
    sync_db_session.rollback()


@pytest.mark.asyncio
async def test_frozen_result_is_independent_of_later_source_changes(
    sync_db_session: Session,
) -> None:
    from sqlalchemy import update
    from apflow.core.storage.sqlalchemy.models import TaskModel

    repository, identifiers = await _failed_run(sync_db_session)
    receipt = await RunRetryModule(repository).execute(
        {"run_id": identifiers[0], "idempotency_key": "retry-1"}
    )
    # Simulate an external legacy writer; supported writers reject this mutation.
    sync_db_session.execute(
        update(TaskModel).where(TaskModel.id == identifiers[1]).values(result={"new": 9})
    )
    sync_db_session.commit()
    frozen = await repository.get_task_by_id(receipt["task_mapping"][identifiers[1]])
    assert frozen is not None
    assert (await repository.resolve_effective_task(frozen)).result == {"value": [7]}


@pytest.mark.asyncio
async def test_unknown_outcome_requires_admin_replay_evidence(sync_db_session: Session) -> None:
    from sqlalchemy import update
    from apflow.core.storage.sqlalchemy.models import WorkflowRun

    repository, identifiers = await _failed_run(sync_db_session)
    sync_db_session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == identifiers[0])
        .values(status="reconciliation_required")
    )
    sync_db_session.commit()
    request = {"run_id": identifiers[0], "idempotency_key": "retry-unknown"}
    module = RunRetryModule(repository)
    with pytest.raises(ModuleError) as unknown:
        await module.execute(request)
    assert unknown.value.code == "RECONCILIATION_REQUIRED"
    approved = {**request, "replay_safe": True, "evidence_ref": "receipt:operator-123"}
    with pytest.raises(ModuleError) as denied:
        await module.execute(approved, Context.create(identity=Identity(id="alice")))
    assert denied.value.code == "ACL_DENIED"
    receipt = await module.execute(
        approved, Context.create(identity=Identity(id="alice", roles=("apflow.admin",)))
    )
    assert receipt["run_id"] != identifiers[0]
    original = await RunGetModule(repository).execute({"run_id": identifiers[0]})
    assert original["status"] == "reconciliation_required"


@pytest.mark.asyncio
async def test_targeted_retry_excludes_unrelated_work(sync_db_session: Session) -> None:
    repository, identifiers = await _failed_run(sync_db_session)
    # Build another legacy terminal run with an independent unfinished branch.
    root = await repository.create_task(name="Root 2", status="completed")
    first = await repository.create_task(
        name="Selected", parent_id=root.id, task_tree_id=root.id, status="failed"
    )
    last = await repository.create_task(
        name="Downstream", parent_id=root.id, task_tree_id=root.id, dependencies=[first.id]
    )
    unrelated = await repository.create_task(
        name="Unrelated", parent_id=root.id, task_tree_id=root.id
    )
    await RunService(sync_db_session).ensure_run(root.id)
    receipt = await RunRetryModule(repository).execute(
        {
            "run_id": root.id,
            "idempotency_key": "selected",
            "task_ids": [first.id],
        }
    )
    assert set(receipt["task_mapping"]) == {root.id, first.id, last.id}
    assert unrelated.id not in receipt["task_mapping"]


@pytest.mark.asyncio
async def test_retry_uses_accepted_input_snapshot(sync_db_session: Session) -> None:
    from sqlalchemy import update
    from apflow.core.storage.sqlalchemy.models import TaskModel

    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="Root", inputs={"input": "original"})
    await RunService(sync_db_session).ensure_run(root.id)
    sync_db_session.execute(
        update(TaskModel)
        .where(TaskModel.id == root.id)
        .values(inputs={"injected": "runtime"}, status="failed")
    )
    sync_db_session.commit()
    await RunService(sync_db_session).finish_run(root.id, "failed")
    receipt = await RunRetryModule(repository).execute(
        {"run_id": root.id, "idempotency_key": "snapshot"}
    )
    copied = await repository.get_task_by_id(receipt["root_task_id"])
    assert copied is not None and copied.inputs == {"input": "original"}


@pytest.mark.asyncio
async def test_retry_invalidates_completed_downstream_of_reexecuted_task(
    sync_db_session: Session,
) -> None:
    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="Root", status="completed")
    failed = await repository.create_task(
        name="B", parent_id=root.id, task_tree_id=root.id, status="failed"
    )
    completed = await repository.create_task(
        name="C",
        parent_id=root.id,
        task_tree_id=root.id,
        dependencies=[failed.id],
        status="completed",
        result={"old": True},
    )
    await RunService(sync_db_session).ensure_run(root.id)
    receipt = await RunRetryModule(repository).execute(
        {"run_id": root.id, "idempotency_key": "invalidate"}
    )
    copied = await repository.get_task_by_id(receipt["task_mapping"][completed.id])
    assert copied is not None and copied.status == "pending" and copied.result is None
    assert copied.id in receipt["execution_task_ids"]


@pytest.mark.asyncio
async def test_retry_rolls_back_new_nodes_when_run_creation_fails(
    sync_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import select, func
    from apflow.core.storage.sqlalchemy.models import TaskModel, WorkflowRun, SubmissionReceipt

    repository, identifiers = await _failed_run(sync_db_session)

    async def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Injected run creation interruption")

    monkeypatch.setattr(RunService, "ensure_run", fail)
    with pytest.raises(RuntimeError, match="Injected"):
        await RunRetryModule(repository).execute(
            {"run_id": identifiers[0], "idempotency_key": "atomic"}
        )
    assert sync_db_session.scalar(select(func.count()).select_from(TaskModel)) == 4
    assert sync_db_session.scalar(select(func.count()).select_from(WorkflowRun)) == 1
    assert sync_db_session.scalar(select(func.count()).select_from(SubmissionReceipt)) == 0


@pytest.mark.asyncio
async def test_accepted_run_definition_is_immutable_and_late_billing_allowed(
    sync_db_session: Session,
) -> None:
    from apflow.core.execution.errors import ValidationError

    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="Accepted")
    await RunService(sync_db_session).ensure_run(root.id)
    with pytest.raises(ValidationError, match="definition"):
        await repository.update_task(root.id, name="Changed")
    root = await repository.get_task_by_id(root.id)
    assert root is not None
    root.inputs = {"changed": True}
    with pytest.raises(ValidationError, match="definition"):
        sync_db_session.commit()
    sync_db_session.rollback()
    await RunService(sync_db_session).finish_run(root.id, "failed")
    await repository.update_task(
        root.id, token_usage={"total_tokens": 2}, actual_cost_usd=Decimal("0.1")
    )
    root = await repository.get_task_by_id(root.id)
    assert root is not None and root.token_usage == {"total_tokens": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["definition_snapshot", "definition_hash", "status", "retry_of_run_id"]
)
async def test_run_history_cannot_be_rewritten(sync_db_session: Session, field: str) -> None:
    from apflow.core.execution.errors import ValidationError
    from apflow.core.storage.sqlalchemy.models import WorkflowRun

    _, identifiers = await _failed_run(sync_db_session)
    run = sync_db_session.get(WorkflowRun, identifiers[0])
    assert run is not None
    setattr(run, field, {} if field == "definition_snapshot" else "changed")
    with pytest.raises(ValidationError, match="history is immutable"):
        sync_db_session.commit()
    sync_db_session.rollback()


def test_concurrent_retry_commits_one_new_run(tmp_path: "Path") -> None:
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import create_engine, select, func
    from sqlalchemy.orm import sessionmaker
    from apflow.core.storage.sqlalchemy.models import Base, TaskModel, WorkflowRun

    engine = create_engine(f"sqlite:///{tmp_path / 'retry-race.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        _, identifiers = asyncio.run(_failed_run(session))

    def submit() -> dict[str, Any]:
        with factory() as session:
            return asyncio.run(
                RunRetryModule(TaskRepository(session)).execute(
                    {
                        "run_id": identifiers[0],
                        "idempotency_key": "concurrent",
                    }
                )
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            receipts = [future.result(timeout=15) for future in futures]
        assert receipts[0] == receipts[1]
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(TaskModel)) == 8
            assert session.scalar(select(func.count()).select_from(WorkflowRun)) == 2
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_copy_starts_fresh_without_historical_charges(sync_db_session: Session) -> None:
    repository = TaskRepository(sync_db_session)
    source = await repository.create_task(
        name="Completed",
        status="completed",
        result={"value": 7},
        token_usage={"total_tokens": 10},
        actual_cost_usd=Decimal("1.5"),
        attempt_id=3,
        attempt_count=2,
    )
    copied = (await TaskCreator(sync_db_session).from_copy(source, _recursive=False)).task
    assert copied.status == "pending"
    assert copied.result is None
    assert copied.token_usage is None
    assert copied.actual_cost_usd is None
    assert copied.attempt_id == copied.attempt_count == 0
    assert copied.completed_at is None
    assert source.status == "completed"
    assert source.token_usage == {"total_tokens": 10}


@pytest.mark.asyncio
async def test_link_does_not_copy_historical_charges(sync_db_session: Session) -> None:
    repository = TaskRepository(sync_db_session)
    source = await repository.create_task(
        name="Completed",
        status="completed",
        result=7,
        token_usage={"total_tokens": 10},
        actual_cost_usd=Decimal("1.5"),
    )
    linked = (await TaskCreator(sync_db_session).from_link(source, _recursive=False)).task
    assert linked.status == "completed"
    assert linked.token_usage is None
    assert linked.actual_cost_usd is None
    assert (await repository.resolve_effective_task(linked)).result == 7
    public = await repository.get_task_tree_for_api(linked)
    assert public.task.token_usage is None and public.task.actual_cost_usd is None


@pytest.mark.asyncio
async def test_retry_rejects_redacted_configuration_instead_of_replaying_placeholder(
    sync_db_session: Session,
) -> None:
    repository = TaskRepository(sync_db_session)
    source = await repository.create_task(
        name="Legacy secret",
        status="failed",
        user_id="alice",
        inputs={"headers": {"Authorization": "Bearer private-value"}},
    )
    await RunService(sync_db_session).ensure_run(source.id)
    with pytest.raises(ModuleError, match="credential_ref"):
        await RunRetryModule(repository).execute(
            {"run_id": source.id, "idempotency_key": "redacted"},
            Context.create(identity=Identity(id="alice")),
        )
    assert len(await repository.query_tasks(user_id="alice")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "replacement-identity"),
        ("max_attempts", 9),
        ("backoff_base_seconds", 50),
        ("placement_constraints", {"region": "changed"}),
    ],
)
async def test_accepted_run_protects_node_identity_and_execution_policy(
    sync_db_session: Session,
    field: str,
    value: Any,
) -> None:
    from apflow.core.execution.errors import ValidationError

    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="Accepted policy")
    await RunService(sync_db_session).ensure_run(task.id)
    setattr(task, field, value)
    with pytest.raises(ValidationError, match="immutable"):
        sync_db_session.commit()
    sync_db_session.rollback()


@pytest.mark.asyncio
async def test_retry_enrolls_legacy_failed_tree_without_resetting_history(
    sync_db_session: Session,
) -> None:
    repository = TaskRepository(sync_db_session)
    original = await repository.create_task(
        name="Legacy failure",
        user_id="alice",
        status="failed",
        error="Historical failure",
        token_usage={"total": 7},
        params={"executor_id": "aggregate_results_executor"},
    )
    receipt = await RunRetryModule(repository).execute(
        {"run_id": original.id, "idempotency_key": "legacy-retry"},
        Context.create(identity=Identity(id="alice")),
    )
    source = await repository.get_task_by_id(original.id)
    assert source is not None and source.status == "failed"
    assert source.error == "Historical failure" and source.token_usage == {"total": 7}
    assert source.run_id == original.id
    assert receipt["run_id"] != original.id and receipt["retry_of_run_id"] == original.id


@pytest.mark.asyncio
async def test_core_retry_requires_administrator_for_uncertain_replay(
    sync_db_session: Session,
) -> None:
    from apflow.core.execution.run_retry import retry_run
    from apflow.core.storage.sqlalchemy.models import WorkflowRun
    from sqlalchemy import update

    repository, identifiers = await _failed_run(sync_db_session)
    sync_db_session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == identifiers[0])
        .values(status="reconciliation_required")
    )
    sync_db_session.commit()
    with pytest.raises(ModuleError) as denied:
        await retry_run(
            repository,
            identifiers[0],
            "core-replay",
            "alice",
            replay_safe=True,
            evidence_ref="receipt:confirmed-safe",
        )
    assert denied.value.code == "ACL_DENIED"


@pytest.mark.asyncio
async def test_targeted_retry_includes_dependencies_of_structural_ancestors(
    sync_db_session: Session,
) -> None:
    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="Root", status="completed")
    prerequisite = await repository.create_task(
        name="Ancestor prerequisite",
        parent_id=root.id,
        task_tree_id=root.id,
        status="completed",
        result={"value": 1},
    )
    group = await repository.create_task(
        name="Group",
        parent_id=root.id,
        task_tree_id=root.id,
        status="completed",
        dependencies=[prerequisite.id],
    )
    failed = await repository.create_task(
        name="Failed", parent_id=group.id, task_tree_id=root.id, status="failed"
    )
    await RunService(sync_db_session).ensure_run(root.id)
    receipt = await RunRetryModule(repository).execute(
        {"run_id": root.id, "idempotency_key": "ancestor", "task_ids": [failed.id]}
    )
    mapping = receipt["task_mapping"]
    assert set(mapping) == {root.id, prerequisite.id, group.id, failed.id}
    copied_group = await repository.get_task_by_id(mapping[group.id])
    assert copied_group is not None
    assert copied_group.dependencies == [mapping[prerequisite.id]]
    assert mapping[prerequisite.id] in receipt["reused_task_ids"]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_managed_run_cannot_gain_existing_unmanaged_child(
    sync_db_session: Session, terminal: bool
) -> None:
    from apflow.core.execution.errors import ValidationError

    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="Managed", status="failed" if terminal else "pending")
    unrelated = await repository.create_task(name="Unmanaged")
    await RunService(sync_db_session).ensure_run(root.id)
    with pytest.raises(ValidationError, match="cannot change children"):
        await repository.update_task(unrelated.id, parent_id=root.id)
    unchanged = await repository.get_task_by_id(unrelated.id)
    assert unchanged is not None and unchanged.parent_id is None


@pytest.mark.asyncio
async def test_historical_billing_uses_mapped_fields_with_extended_table(
    sync_db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import Column, MetaData, String, Table

    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="Historical", status="failed")
    await RunService(sync_db_session).ensure_run(root.id)
    extended_table = cast(Table, root.__table__).to_metadata(MetaData())
    extended_table.append_column(Column("custom_extension", String))
    # Custom models may extend the shared table without changing the base mapper.
    monkeypatch.setattr(root, "__table__", extended_table)
    root.token_usage = {"total_tokens": 3}
    sync_db_session.commit()
    sync_db_session.refresh(root)
    assert root.token_usage == {"total_tokens": 3}
    assert root.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("invocation_status", ["running", "unknown"])
async def test_retry_checks_unknown_physical_invocation_without_task_error_projection(
    sync_db_session: Session, invocation_status: str
) -> None:
    from datetime import datetime, timezone
    from apflow.core.storage.sqlalchemy.models import ExecutionAttempt

    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="Legacy failure", user_id="alice", status="failed")
    sync_db_session.add(
        ExecutionAttempt(
            id="legacy-unknown",
            task_id=root.id,
            attempt_id=0,
            attempt_number=1,
            status=invocation_status,
            started_at=datetime.now(timezone.utc),
        )
    )
    sync_db_session.commit()
    with pytest.raises(ModuleError) as unknown:
        await RunRetryModule(repository).execute(
            {"run_id": root.id, "idempotency_key": "unknown-invocation"},
            Context.create(identity=Identity(id="alice")),
        )
    assert unknown.value.code == "RECONCILIATION_REQUIRED"
    receipt = await RunRetryModule(repository).execute(
        {
            "run_id": root.id,
            "idempotency_key": "authorized-unknown-invocation",
            "replay_safe": True,
            "evidence_ref": "provider:verified-safe",
        },
        Context.create(identity=Identity(id="alice", roles=("apflow.admin",))),
    )
    assert receipt["run_id"] != root.id
    sync_db_session.refresh(root)
    invocation = sync_db_session.get(ExecutionAttempt, "legacy-unknown")
    assert root.status == "failed" and root.error_info is None
    assert invocation is not None and invocation.status == invocation_status
