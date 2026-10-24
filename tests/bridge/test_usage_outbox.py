"""Usage delivery is scoped to its owner and remains idempotent."""

from types import SimpleNamespace

import pytest
from apcore.errors import ModuleError
from sqlalchemy.orm import Session

from apflow.bridge.governance_modules import UsageAcknowledgeModule, UsageEventsModule
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.durability.attempts import AttemptRecorder
from apflow.governance.usage import UsageRecorder


def caller(owner: str) -> SimpleNamespace:
    return SimpleNamespace(identity=SimpleNamespace(id=owner, roles=[]))


@pytest.mark.asyncio
async def test_usage_outbox_preserves_owner_boundary(sync_db_session: Session) -> None:
    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="Metered", user_id="alice")
    invocation = await AttemptRecorder(sync_db_session).start(task.id, 0, 1)
    await AttemptRecorder(sync_db_session).finish(invocation, "completed", token_usage={"total": 5})
    module = UsageEventsModule(repository)
    alice = await module.execute({}, caller("alice"))
    assert len(alice["events"]) == 1
    assert alice["events"][0]["invocation_id"] == invocation
    assert (await module.execute({}, caller("bob")))["events"] == []
    cursor = alice["next_index"] + 10
    late = await module.execute({"after_index": cursor}, caller("alice"))
    assert len(late["events"]) == 1
    assert late["next_index"] == cursor
    event_id = alice["events"][0]["id"]
    acknowledgement = UsageAcknowledgeModule(repository)
    with pytest.raises(ModuleError):
        await acknowledgement.execute({"event_id": event_id}, caller("bob"))
    await acknowledgement.execute({"event_id": event_id}, caller("alice"))
    await acknowledgement.execute({"event_id": event_id}, caller("alice"))
    events = await UsageRecorder(sync_db_session).list_events("alice")
    assert events[0]["delivered_at"] is not None
    assert events[0]["token_usage"]["total"] == 5


@pytest.mark.asyncio
async def test_budget_reconciliation_delivers_confirmed_usage(sync_db_session: Session) -> None:
    from apflow.bridge.governance_modules import BudgetReconcileModule
    from apflow.governance.ledger import BudgetLedger, scope_budget_key

    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="Unknown delivery", user_id="alice")
    recorder = AttemptRecorder(sync_db_session)
    invocation = await recorder.start(task.id, 0, 1)
    ledger = BudgetLedger(sync_db_session)
    key = scope_budget_key("alice", "scope")
    await ledger.configure_scope(key, 100)
    await ledger.reserve(key, task.id, invocation, 10)
    await recorder.finish(invocation, "unknown")
    admin = SimpleNamespace(identity=SimpleNamespace(id="operator", roles=["apflow.admin"]))
    inputs = {
        "scope": "scope",
        "owner_id": "alice",
        "reservation_id": invocation,
        "actual_tokens": 5,
        "evidence_ref": "receipt:confirmed",
    }
    module = BudgetReconcileModule(repository)
    await module.execute(inputs, admin)
    await module.execute(inputs, admin)
    events = await UsageRecorder(sync_db_session).list_events("alice")
    assert len(events) == 1 and events[0]["token_usage"]["total"] == 5
    assert events[0]["evidence"]["evidence_ref"] == "receipt:confirmed"
    assert (await ledger.get_scope(key)).spent_tokens == 5
