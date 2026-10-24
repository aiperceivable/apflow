"""Shared balances serialize parallel reservations and retain unknown liabilities."""

from typing import Any
from sqlalchemy import Engine
from collections.abc import Iterator
from pathlib import Path

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from apflow.core.execution.errors import ConfigurationError
from apflow.core.storage.sqlalchemy.models import (
    Base,
    SubmissionReceipt,
    TaskModel,
    ExecutionAttempt,
)
from apflow.governance.budget import BudgetManager
from apflow.governance.ledger import BudgetExceededError, BudgetLedger, scope_budget_key


@pytest.fixture
def budget_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}", connect_args={"timeout": 10})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        asyncio.run(BudgetLedger(session).configure_scope("experiment", 100))
    yield engine
    engine.dispose()


def test_parallel_candidates_share_atomic_limit(budget_engine: Engine) -> None:
    def reserve(identifier: str) -> bool:
        with Session(budget_engine) as session:
            try:
                asyncio.run(BudgetLedger(session).reserve("experiment", identifier, identifier, 60))
                return True
            except BudgetExceededError:
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, ["a", "b"])) == [False, True]
    with Session(budget_engine) as session:
        snapshot = asyncio.run(BudgetLedger(session).get_scope("experiment"))
        assert snapshot.reserved_tokens == 60
        assert snapshot.remaining_tokens == 40


@pytest.mark.asyncio
async def test_reservation_and_settlement_are_idempotent(budget_engine: Engine) -> None:
    with Session(budget_engine) as session:
        ledger = BudgetLedger(session)
        await ledger.reserve("experiment", "t", "attempt", 60)
        await ledger.reserve("experiment", "t", "attempt", 60)
        await ledger.settle("attempt", 35)
        await ledger.settle("attempt", 35)
        snapshot = await ledger.get_scope("experiment")
        assert (snapshot.spent_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (
            35,
            0,
            0,
        )
        with pytest.raises(ValueError, match="already settled"):
            await ledger.settle("attempt", 36)


@pytest.mark.asyncio
async def test_unknown_charge_remains_held_until_reconciled(budget_engine: Engine) -> None:
    with Session(budget_engine) as session:
        await BudgetLedger(session).reserve("experiment", "t", "attempt", 80)
        await BudgetLedger(session).settle("attempt", None)
    with Session(budget_engine) as session:
        ledger = BudgetLedger(session)
        snapshot = await ledger.get_scope("experiment")
        assert snapshot.unknown_tokens == 80 and snapshot.remaining_tokens == 20
        with pytest.raises(BudgetExceededError):
            await ledger.reserve("experiment", "next", "next-attempt", 30)
        await ledger.settle("attempt", 50)
        assert (await ledger.get_scope("experiment")).remaining_tokens == 50


@pytest.mark.asyncio
async def test_scope_cannot_be_reset_or_key_rebound(budget_engine: Engine) -> None:
    with Session(budget_engine) as session:
        ledger = BudgetLedger(session)
        with pytest.raises(ValueError, match="different limit"):
            await ledger.configure_scope("experiment", 200)
        await ledger.reserve("experiment", "t", "attempt", 60)
        with pytest.raises(ValueError, match="different invocation"):
            await ledger.reserve("experiment", "other", "attempt", 60)
    assert scope_budget_key("alice", "exp") != scope_budget_key("bob", "exp")
    assert scope_budget_key(None, "exp") != scope_budget_key("alice", "exp")
    with pytest.raises(ValueError, match="owner"):
        scope_budget_key("", "exp")


@pytest.mark.asyncio
async def test_actual_invocation_requires_bound_and_settles_failed_usage(
    budget_engine: Engine,
) -> None:
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    called = 0

    async def invoke() -> dict:
        nonlocal called
        called += 1
        return {"result": None, "error": "Partial failure", "token_usage": {"total": 30}}

    with Session(budget_engine) as session:
        task = TaskModel(id="t", name="bounded", budget_scope_id="experiment")
        manager = BudgetManager(TaskRepository(session))
        with pytest.raises(ConfigurationError, match="bound"):
            await manager.execute_with_reservation(task, invoke)
        assert called == 0
        await manager.execute_with_reservation(task, invoke, token_bound=40)
        assert called == 1
        snapshot = await BudgetLedger(session).get_scope("experiment")
        assert snapshot.spent_tokens == 30 and snapshot.reserved_tokens == 0


@pytest.mark.asyncio
async def test_cancelled_invocation_retains_unknown_charge(budget_engine: Engine) -> None:
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    async def invoke() -> None:
        raise asyncio.CancelledError()

    with Session(budget_engine) as session:
        task = TaskModel(id="t", name="bounded", budget_scope_id="experiment")
        manager = BudgetManager(TaskRepository(session))
        with pytest.raises(asyncio.CancelledError):
            await manager.execute_with_reservation(task, invoke, token_bound=70)
        assert (await BudgetLedger(session).get_scope("experiment")).unknown_tokens == 70


@pytest.mark.asyncio
async def test_missing_usage_and_explicit_zero_are_distinct(budget_engine: Engine) -> None:
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    with Session(budget_engine) as session:
        manager = BudgetManager(TaskRepository(session))
        task = TaskModel(id="t", name="bounded", budget_scope_id="experiment")

        async def no_usage() -> dict:
            return {"result": "ok", "token_usage": {}}

        async def zero_usage() -> dict:
            return {"result": "ok", "token_usage": {"total": 0}}

        await manager.execute_with_reservation(task, no_usage, token_bound=40)
        await manager.execute_with_reservation(task, zero_usage, token_bound=40)
        snapshot = await BudgetLedger(session).get_scope("experiment")
        assert snapshot.unknown_tokens == 40 and snapshot.remaining_tokens == 60


@pytest.mark.asyncio
async def test_exceeded_executor_bound_is_reported_without_hiding_spend(
    budget_engine: Engine,
) -> None:
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    async def invoke() -> dict:
        return {"result": "ok", "token_usage": {"total": 20}}

    with Session(budget_engine) as session:
        task = TaskModel(id="t", name="bounded", budget_scope_id="experiment")
        manager = BudgetManager(TaskRepository(session))
        with pytest.raises(ConfigurationError, match="exceeded"):
            await manager.execute_with_reservation(task, invoke, token_bound=10)
        snapshot = await BudgetLedger(session).get_scope("experiment")
        assert snapshot.spent_tokens == 20 and snapshot.reserved_tokens == 0
        assert session.scalars(select(ExecutionAttempt)).one().status == "failed"


def test_concurrent_duplicate_settlement_does_not_double_charge(budget_engine: Engine) -> None:
    with Session(budget_engine) as session:
        asyncio.run(BudgetLedger(session).reserve("experiment", "t", "attempt", 60))

    def settle(_: int) -> None:
        with Session(budget_engine) as session:
            asyncio.run(BudgetLedger(session).settle("attempt", 20))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(settle, [0, 1]))
    with Session(budget_engine) as session:
        snapshot = asyncio.run(BudgetLedger(session).get_scope("experiment"))
        assert snapshot.spent_tokens == 20 and snapshot.reserved_tokens == 0


@pytest.mark.asyncio
async def test_reconciliation_evidence_is_atomic_durable_and_immutable(
    budget_engine: Engine,
) -> None:
    evidence = {
        "evidence_ref": "provider:receipt-1",
        "actor": "operator",
        "observed_at": "2026-10-24T00:00:00+00:00",
    }
    with Session(budget_engine) as session:
        ledger = BudgetLedger(session)
        await ledger.reserve("experiment", "t", "attempt", 60)
        await ledger.settle("attempt", None)
        await ledger.settle("attempt", 25, reconciliation=evidence)
        await ledger.settle(
            "attempt", 25, reconciliation={**evidence, "evidence_ref": "replacement"}
        )
        with pytest.raises(ValueError, match="already settled"):
            await ledger.settle("attempt", 26, reconciliation=evidence)
    with Session(budget_engine) as session:
        audit = session.get(SubmissionReceipt, ("budget-reconciliation:experiment", "attempt"))
        assert audit is not None
        assert audit.receipt["evidence_ref"] == "provider:receipt-1"
        assert audit.receipt["actor"] == "operator" and audit.receipt["actual_tokens"] == 25
        snapshot = await BudgetLedger(session).get_scope("experiment")
        assert snapshot.spent_tokens == 25 and snapshot.unknown_tokens == 0


@pytest.mark.asyncio
async def test_reconciliation_audit_failure_rolls_back_balance(
    budget_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = {
        "evidence_ref": "provider:receipt",
        "actor": "operator",
        "observed_at": "2026-10-24T00:00:00+00:00",
    }

    async def fail_audit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Evidence store unavailable")

    with Session(budget_engine) as session:
        ledger = BudgetLedger(session)
        await ledger.reserve("experiment", "t", "attempt", 60)
        await ledger.settle("attempt", None)
        monkeypatch.setattr(ledger, "_record_reconciliation", fail_audit)
        with pytest.raises(RuntimeError, match="Evidence store"):
            await ledger.settle("attempt", 20, reconciliation=evidence)
        snapshot = await ledger.get_scope("experiment")
        assert snapshot.spent_tokens == 0 and snapshot.unknown_tokens == 60
        reservation = await ledger.get_reservation("experiment", "attempt")
        assert reservation is not None and reservation["state"] == "unknown"


@pytest.mark.asyncio
async def test_denied_reservation_does_not_leave_running_invocation(budget_engine: Engine) -> None:
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    called = False

    async def invoke() -> dict[str, Any]:
        nonlocal called
        called = True
        return {"result": "unexpected"}

    with Session(budget_engine) as session:
        task = TaskModel(id="denied", name="Denied", budget_scope_id="experiment")
        manager = BudgetManager(TaskRepository(session))
        with pytest.raises(BudgetExceededError):
            await manager.execute_with_reservation(task, invoke, token_bound=1000)
        assert not called
        assert session.scalars(select(ExecutionAttempt)).one().status == "failed"
