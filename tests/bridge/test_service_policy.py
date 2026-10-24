"""Deployment policies apply consistently to native ACLs and service definitions."""

from pathlib import Path
from typing import Any

import pytest
from apcore import Context, Identity
from apcore.errors import ModuleError

from apflow.core.config_manager import get_config_manager


@pytest.fixture
def policy_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    configuration = get_config_manager()
    monkeypatch.setattr(configuration, "_data", dict(configuration._data))


def test_native_acl_uses_actual_executor_arguments(
    tmp_path: Path, policy_configuration: None
) -> None:
    from apflow.bridge.access import authorize_capability

    policy = tmp_path / "acl.yaml"
    policy.write_text(
        'version: "1.0"\ndefault_effect: deny\nrules:\n  - callers: ["*"]\n    targets: ["training.executor"]\n    effect: allow\n    conditions:\n      arguments:\n        has_all_keys: ["url"]\n'
    )
    get_config_manager().set("security.acl_path", str(policy))
    context = Context.create(identity=Identity(id="alice"))
    authorize_capability("training.executor", context, inputs={"url": "https://world.example"})
    with pytest.raises(ModuleError):
        authorize_capability("training.executor", context, inputs={"task_id": "task"})


def test_native_acl_approval_does_not_silently_allow(
    tmp_path: Path, policy_configuration: None
) -> None:
    from apflow.bridge.access import authorize_capability

    policy = tmp_path / "acl.yaml"
    policy.write_text(
        'version: "1.0"\ndefault_effect: deny\nrules:\n  - callers: ["*"]\n    targets: ["apflow.rest_executor"]\n    effect: allow\n    approval: required\n'
    )
    get_config_manager().set("security.acl_path", str(policy))
    with pytest.raises(ModuleError) as failure:
        authorize_capability("apflow.rest_executor", Context.create(identity=Identity(id="alice")))
    assert failure.value.code == "ACL_DENIED"


@pytest.mark.asyncio
async def test_custom_namespace_authorization_and_receipts_are_consistent(
    sync_db_session: Any, policy_configuration: None
) -> None:
    from apflow.bridge.registry_setup import create_apflow_registry
    from apflow.core.execution.task_creator import TaskCreator
    from apflow.core.execution.task_manager import TaskManager
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    repository = TaskRepository(sync_db_session)
    arguments = (TaskManager(sync_db_session), TaskCreator(sync_db_session), repository)
    training = create_apflow_registry(*arguments, namespace="training")
    other = create_apflow_registry(*arguments, namespace="other")
    context = Context.create(identity=Identity(id="alice"))
    get_config_manager().set("security.allowed_modules", ["training.*", "other.*"])
    request = {"name": "created", "idempotency_key": "same-operation"}
    first = await training.get("training.task.create").execute(request, context)
    second = await other.get("other.task.create").execute(request, context)
    assert first["id"] != second["id"]
    receipt = await training.get("training.task.receipt").execute(
        {"idempotency_key": "same-operation"}, context
    )
    assert receipt == first


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["accepted", "running", "cancel_requested", "reconciliation_required"]
)
@pytest.mark.parametrize("operation", ["update", "delete", "schedule", "execute"])
async def test_active_dispatch_freezes_public_definition_mutations(
    sync_db_session: Any, state: str, operation: str
) -> None:
    from apflow.bridge.schedule_modules import ScheduleSetModule
    from apflow.bridge.task_modules import TaskDeleteModule, TaskExecuteModule, TaskUpdateModule
    from apflow.core.storage.sqlalchemy.models import TaskDispatch
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="accepted-definition", user_id="alice")
    sync_db_session.add(TaskDispatch(task_id=task.id, state=state))
    sync_db_session.commit()
    context = Context.create(identity=Identity(id="alice"))
    if operation == "update":
        module = TaskUpdateModule(repository)
        inputs = {"task_id": task.id, "inputs": {"changed": True}}
    elif operation == "delete":
        module = TaskDeleteModule(repository)
        inputs = {"task_id": task.id}
    elif operation == "schedule":
        module = ScheduleSetModule(repository)
        inputs = {"task_id": task.id, "schedule_type": "interval", "schedule_expression": "1h"}
    else:
        from apflow.core.execution.task_manager import TaskManager

        module = TaskExecuteModule(TaskManager(sync_db_session))
        inputs = {"task_id": task.id}
    with pytest.raises(ModuleError) as failure:
        await module.execute(inputs, context)
    assert failure.value.code == "TASK_DEFINITION_LOCKED"


@pytest.mark.asyncio
async def test_active_dispatch_still_allows_read_and_cancel(sync_db_session: Any) -> None:
    from apflow.bridge.task_modules import TaskCancelModule, TaskGetModule
    from apflow.core.execution.task_manager import TaskManager
    from apflow.core.storage.sqlalchemy.models import TaskDispatch
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="accepted-definition", user_id="alice")
    sync_db_session.add(TaskDispatch(task_id=task.id, state="accepted"))
    sync_db_session.commit()
    context = Context.create(identity=Identity(id="alice"))
    assert (await TaskGetModule(repository).execute({"task_id": task.id}, context))["id"] == task.id
    result = await TaskCancelModule(TaskManager(sync_db_session)).execute(
        {"task_ids": [task.id]}, context
    )
    assert result["results"]


@pytest.mark.asyncio
async def test_native_acl_receives_inner_arguments_at_real_execution_boundary(
    sync_db_session: Any, tmp_path: Path, policy_configuration: None
) -> None:
    from apflow.bridge.access import execution_context
    from apflow.core.base import BaseTask
    from apflow.core.execution.task_manager import TaskManager
    from apflow.core.extensions import get_registry
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    class PolicyProbe(BaseTask):
        id = "policy_probe_executor"
        name = "Policy probe"
        description = "Exercise the actual executor authorization boundary"
        result_format = "envelope"

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            return {"result": {"observed": inputs["url"]}}

    policy = tmp_path / "execution-acl.yaml"
    policy.write_text(
        'version: "1.0"\ndefault_effect: deny\nrules:\n  - callers: ["apflow.task.execute"]\n    targets: ["apflow.policy_probe_executor"]\n    effect: allow\n    conditions:\n      arguments:\n        has_all_keys: ["url"]\n'
    )
    get_config_manager().set("security.acl_path", str(policy))
    registry = get_registry()
    registry.register(PolicyProbe(), executor_class=PolicyProbe)
    context = Context.create(identity=Identity(id="alice"))
    context.call_chain = ["apflow.task.execute"]
    token = execution_context.set(context)
    try:
        task = await TaskRepository(sync_db_session).create_task(
            name="policy", params={"executor_id": PolicyProbe.id}
        )
        manager = TaskManager(sync_db_session)
        result = await manager._execute_task_with_schemas(task, {"url": "https://world.example"})
        assert result["result"] == {"observed": "https://world.example"}
        with pytest.raises(ModuleError):
            await manager._execute_task_with_schemas(task, {"task_id": task.id})
    finally:
        execution_context.reset(token)
        registry.unregister(PolicyProbe.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [" ", "x" * 1025, "receipt\nforged-log"])
async def test_reconciliation_rejects_invalid_evidence_before_changing_dispatch(
    sync_db_session: Any, evidence: str
) -> None:
    from apflow.bridge.execution_modules import TaskReconcileModule
    from apflow.core.storage.sqlalchemy.models import TaskDispatch
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="unknown", user_id="alice", status="failed")
    sync_db_session.add(TaskDispatch(task_id=task.id, state="reconciliation_required"))
    sync_db_session.commit()
    context = Context.create(identity=Identity(id="operator", roles=("apflow.admin",)))
    with pytest.raises(ModuleError) as failure:
        await TaskReconcileModule(repository).execute(
            {"task_id": task.id, "replay_safe": True, "evidence_ref": evidence}, context
        )
    assert failure.value.code == "INVALID_INPUT"
    assert sync_db_session.get(TaskDispatch, task.id).state == "reconciliation_required"


@pytest.mark.asyncio
async def test_budget_admin_reconciliation_is_scoped_and_audited(sync_db_session: Any) -> None:
    from sqlalchemy import select
    from apflow.bridge.governance_modules import BudgetReconcileModule, BudgetScopeModule
    from apflow.core.storage.sqlalchemy.models import SubmissionReceipt
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
    from apflow.governance.ledger import BudgetLedger, scope_budget_key

    repository = TaskRepository(sync_db_session)
    alice = Context.create(identity=Identity(id="alice"))
    bob = Context.create(identity=Identity(id="bob"))
    admin = Context.create(identity=Identity(id="operator", roles=("apflow.admin",)))
    scopes = BudgetScopeModule(repository)
    await scopes.execute({"scope": "experiment", "limit_tokens": 100}, alice)
    with pytest.raises(ModuleError):
        await scopes.execute({"scope": "experiment", "owner_id": "alice"}, bob)
    ledger = BudgetLedger(sync_db_session)
    key = scope_budget_key("alice", "experiment")
    await ledger.reserve(key, "synthetic-task", "reservation-1", 50)
    await ledger.settle("reservation-1", None)
    request = {
        "scope": "experiment",
        "owner_id": "alice",
        "reservation_id": "reservation-1",
        "actual_tokens": 20,
        "evidence_ref": "receipt:world-1",
    }
    with pytest.raises(ModuleError):
        await BudgetReconcileModule(repository).execute(request, bob)
    module = BudgetReconcileModule(repository)
    await module.execute(request, admin)
    repeated = await module.execute({**request, "evidence_ref": "receipt:replacement"}, admin)
    assert repeated["evidence_ref"] == "receipt:world-1"
    with pytest.raises(ModuleError) as conflict:
        await module.execute({**request, "actual_tokens": 30}, admin)
    assert conflict.value.code == "INVALID_INPUT"
    snapshot = await ledger.get_scope(key)
    assert snapshot.spent_tokens == 20 and snapshot.unknown_tokens == 0
    audit = sync_db_session.execute(
        select(SubmissionReceipt).where(SubmissionReceipt.scope == "budget-reconciliation:" + key)
    ).scalar_one()
    assert audit.receipt["actor"] == "operator"
    assert audit.receipt["evidence_ref"] == "receipt:world-1"


@pytest.mark.asyncio
async def test_native_acl_budget_policy_sees_public_arguments(
    sync_db_session: Any, tmp_path: Path, policy_configuration: None
) -> None:
    from apflow.bridge.governance_modules import BudgetScopeModule
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    policy = tmp_path / "budget-acl.yaml"
    policy.write_text(
        'version: "1.0"\ndefault_effect: deny\nrules:\n  - callers: ["*"]\n    targets: ["apflow.budget.scope"]\n    effect: allow\n    conditions:\n      arguments:\n        has_all_keys: ["scope"]\n'
    )
    get_config_manager().set("security.acl_path", str(policy))
    result = await BudgetScopeModule(TaskRepository(sync_db_session)).execute(
        {"scope": "experiment", "limit_tokens": 100}, Context.create(identity=Identity(id="alice"))
    )
    assert result["limit_tokens"] == 100


@pytest.mark.asyncio
async def test_dispatch_definition_guard_covers_children_and_administrators(
    sync_db_session: Any,
) -> None:
    from apflow.bridge.task_modules import TaskUpdateModule
    from apflow.core.storage.sqlalchemy.models import TaskDispatch
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="root", user_id="alice")
    child = await repository.create_task(name="child", user_id="alice", parent_id=root.id)
    sync_db_session.add(TaskDispatch(task_id=root.id, state="running"))
    sync_db_session.commit()
    administrator = Context.create(identity=Identity(id="operator", roles=("apflow.admin",)))
    with pytest.raises(ModuleError) as failure:
        await TaskUpdateModule(repository).execute(
            {"task_id": child.id, "inputs": {"modified": True}}, administrator
        )
    assert failure.value.code == "TASK_DEFINITION_LOCKED"


@pytest.mark.asyncio
async def test_finished_dispatch_definition_can_be_edited(sync_db_session: Any) -> None:
    from apflow.bridge.task_modules import TaskUpdateModule
    from apflow.core.storage.sqlalchemy.models import TaskDispatch
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="root", user_id="alice")
    sync_db_session.add(TaskDispatch(task_id=task.id, state="finished"))
    sync_db_session.commit()
    output = await TaskUpdateModule(repository).execute(
        {"task_id": task.id, "inputs": {"modified": True}},
        Context.create(identity=Identity(id="alice")),
    )
    assert output["inputs"] == {"modified": True}


@pytest.mark.asyncio
async def test_reconciliation_missing_root_does_not_accept_orphan_dispatch(
    sync_db_session: Any,
) -> None:
    from apflow.bridge.execution_modules import TaskReconcileModule
    from apflow.core.storage.sqlalchemy.models import TaskDispatch
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    sync_db_session.add(TaskDispatch(task_id="missing-root", state="reconciliation_required"))
    sync_db_session.commit()
    context = Context.create(identity=Identity(id="operator", roles=("apflow.admin",)))
    with pytest.raises(ModuleError) as failure:
        await TaskReconcileModule(TaskRepository(sync_db_session)).execute(
            {"task_id": "missing-root", "replay_safe": True, "evidence_ref": "receipt:1"}, context
        )
    assert failure.value.code == "MODULE_NOT_FOUND"
    assert sync_db_session.get(TaskDispatch, "missing-root").state == "reconciliation_required"


@pytest.mark.asyncio
async def test_budget_scope_public_errors_are_client_errors(sync_db_session: Any) -> None:
    from apcore import Executor, Registry
    from apflow.api.rest import _execute
    from apflow.bridge.governance_modules import BudgetScopeModule
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    registry = Registry()
    registry.register("apflow.budget.scope", BudgetScopeModule(TaskRepository(sync_db_session)))
    executor = Executor.from_registry(registry)
    context = Context.create(identity=Identity(id="alice"))
    missing_status, missing = await _execute(
        executor, "apflow.budget.scope", {"scope": "missing"}, context
    )
    assert missing_status == 404
    assert missing["error"]["code"] == "MODULE_NOT_FOUND"
    assert (
        await _execute(
            executor, "apflow.budget.scope", {"scope": "experiment", "limit_tokens": 100}, context
        )
    )[0] == 200
    conflict_status, conflict = await _execute(
        executor, "apflow.budget.scope", {"scope": "experiment", "limit_tokens": 200}, context
    )
    assert conflict_status == 422
    assert conflict["error"]["code"] == "INVALID_INPUT"
