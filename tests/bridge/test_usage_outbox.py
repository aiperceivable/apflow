"""Usage delivery is scoped to its owner and remains idempotent."""

from types import SimpleNamespace
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, BrokenBarrierError
from typing import Any

import pytest
from apcore.errors import ModuleError
from sqlalchemy.orm import Session
from sqlalchemy import create_engine, select

from apflow.bridge.governance_modules import UsageAcknowledgeModule, UsageEventsModule
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.durability.attempts import AttemptRecorder
from apflow.governance.usage import UsageRecorder
from apflow.core.storage.sqlalchemy.models import Base, UsageEvent


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


@pytest.mark.asyncio
async def test_budget_reconciliation_correction_returns_durable_receipt(
    sync_db_session: Session,
) -> None:
    from apflow.bridge.governance_modules import BudgetReconcileModule
    from apflow.governance.ledger import BudgetLedger, scope_budget_key

    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="Corrected delivery", user_id="alice")
    recorder = AttemptRecorder(sync_db_session)
    invocation = await recorder.start(task.id, 0, 1)
    ledger = BudgetLedger(sync_db_session)
    key = scope_budget_key("alice", "scope")
    await ledger.configure_scope(key, 100)
    await ledger.reserve(key, task.id, invocation, 10)
    await recorder.finish(invocation, "completed", token_usage={"total": 7})
    admin = SimpleNamespace(identity=SimpleNamespace(id="operator", roles=["apflow.admin"]))
    inputs = {
        "scope": "scope",
        "owner_id": "alice",
        "reservation_id": invocation,
        "actual_tokens": 5,
        "evidence_ref": "receipt:corrected",
    }
    module = BudgetReconcileModule(repository)
    receipt = await module.execute(inputs, admin)
    assert receipt["evidence_ref"] == "receipt:corrected"
    assert await module.execute(inputs, admin) == receipt
    events = await UsageRecorder(sync_db_session).list_events("alice")
    assert [event["token_usage"]["total"] for event in events] == [7, -2]
    assert (await ledger.get_scope(key)).spent_tokens == 5


def test_concurrent_reconciliation_reuses_committed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apflow.bridge.governance_modules import BudgetReconcileModule
    from apflow.governance.ledger import BudgetLedger, scope_budget_key

    engine = create_engine(f"sqlite:///{tmp_path / 'reconcile.db'}")
    Base.metadata.create_all(engine)
    key = scope_budget_key("alice", "scope")
    with Session(engine) as session:
        repository = TaskRepository(session)
        task = asyncio.run(repository.create_task(name="Unknown receipt", user_id="alice"))
        recorder = AttemptRecorder(session)
        invocation = asyncio.run(recorder.start(task.id, 0, 1))
        ledger = BudgetLedger(session)
        asyncio.run(ledger.configure_scope(key, 100))
        asyncio.run(ledger.reserve(key, task.id, invocation, 10))
        asyncio.run(recorder.finish(invocation, "unknown"))
    evidence_ready = Barrier(2)
    original_evidence = BudgetReconcileModule._evidence

    async def synchronized_evidence(
        self: BudgetReconcileModule, scope: str, inputs: dict[str, Any], context: Any
    ) -> dict[str, Any]:
        evidence = await original_evidence(self, scope, inputs, context)
        try:
            evidence_ready.wait(timeout=1)
        except BrokenBarrierError:
            pass
        return evidence

    monkeypatch.setattr(BudgetReconcileModule, "_evidence", synchronized_evidence)
    admin = SimpleNamespace(identity=SimpleNamespace(id="operator", roles=["apflow.admin"]))
    inputs = {
        "scope": "scope",
        "owner_id": "alice",
        "reservation_id": invocation,
        "actual_tokens": 5,
        "evidence_ref": "receipt:concurrent",
    }

    def reconcile(_: int) -> dict[str, Any]:
        with Session(engine) as session:
            return asyncio.run(
                BudgetReconcileModule(TaskRepository(session)).execute(inputs, admin)
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(reconcile, [0, 1]))
    assert receipts[0] == receipts[1]
    with Session(engine) as session:
        assert len(session.scalars(select(UsageEvent)).all()) == 1
        assert asyncio.run(BudgetLedger(session).get_scope(key)).spent_tokens == 5
    engine.dispose()
