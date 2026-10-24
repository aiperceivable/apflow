"""Failure history retains origin and usage separately from business output."""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import pytest

from apflow.core.execution.errors import http_error_info
from apflow.core.storage.sqlalchemy.models import Base
from apflow.durability.attempts import AttemptRecorder, list_execution_attempts


@pytest.mark.asyncio
async def test_attempt_history_persists_structured_failure_and_retry_time(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    engine = create_engine(f"sqlite:///{tmp_path / 'attempts.db'}")
    Base.metadata.create_all(engine)
    retry_at = datetime.now(timezone.utc)
    with Session(engine) as session:
        recorder = AttemptRecorder(session)
        record = await recorder.start("t", 2, 1)
        await recorder.finish(
            record, "failed", error_info=http_error_info(503, "GET"), token_usage={"total": 12}
        )
        await recorder.schedule_retry(record, retry_at)
        with pytest.raises(ValueError, match="different final usage"):
            await recorder.finish(record, "completed", token_usage={"total": 0})
    with Session(engine) as session:
        rows = await list_execution_attempts("t", session)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert rows[0]["error_info"]["code"] == "HTTP_503"
        assert rows[0]["token_usage"] == {"total": 12}
        assert rows[0]["attempt_id"] == 2 and rows[0]["attempt_number"] == 1
        assert rows[0]["next_retry_at"] is not None
        assert await list_execution_attempts("other", session) == []
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
async def test_missing_terminal_usage_accepts_late_receipt_once(
    sync_db_session: Session, terminal_status: str
) -> None:
    from sqlalchemy import select
    from apflow.core.storage.sqlalchemy.models import ExecutionAttempt, TaskModel, UsageEvent
    from apflow.governance.ledger import BudgetLedger

    sync_db_session.add(TaskModel(id="late", name="Late", status="cancelled", result="old"))
    sync_db_session.commit()
    recorder = AttemptRecorder(sync_db_session)
    invocation = await recorder.start("late", 0, 1)
    ledger = BudgetLedger(sync_db_session)
    await ledger.configure_scope("scope", 100)
    await ledger.reserve("scope", "late", invocation, 20)
    await recorder.finish(invocation, terminal_status)
    await recorder.finish(invocation, "completed", token_usage={"total": 7})
    await recorder.finish(invocation, "completed", token_usage={"total": 7})
    sync_db_session.expire_all()
    attempt = sync_db_session.get(ExecutionAttempt, invocation)
    task = sync_db_session.get(TaskModel, "late")
    assert attempt is not None and attempt.status == terminal_status
    assert attempt.token_usage is None
    assert task is not None and task.result == "old" and task.status == "cancelled"
    assert task.token_usage is not None and task.token_usage["total"] == 7
    assert len(sync_db_session.scalars(select(UsageEvent)).all()) == 1
    balance = await ledger.get_scope("scope")
    assert balance.spent_tokens == 7 and balance.unknown_tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("late_total", [2, 8])
async def test_late_executor_receipt_does_not_replace_operator_reconciliation(
    sync_db_session: Session, late_total: int
) -> None:
    from sqlalchemy import select
    from apflow.core.storage.sqlalchemy.models import TaskModel, UsageEvent
    from apflow.governance.ledger import BudgetLedger
    from apflow.governance.usage import UsageRecorder

    sync_db_session.add(TaskModel(id="late", name="Late"))
    sync_db_session.commit()
    recorder = AttemptRecorder(sync_db_session)
    invocation = await recorder.start("late", 0, 1)
    ledger = BudgetLedger(sync_db_session)
    await ledger.configure_scope("scope", 100)
    await ledger.reserve("scope", "late", invocation, 20)
    await recorder.finish(invocation, "unknown")
    await UsageRecorder(sync_db_session).reconcile(
        invocation,
        "provider-corrected",
        {"total": 4},
        {
            "evidence_ref": "provider:corrected",
            "actor": "operator",
            "observed_at": "2026-10-24T00:00:00+00:00",
        },
    )
    with pytest.raises(ValueError, match="authoritative reconciliation"):
        await recorder.finish(invocation, "completed", token_usage={"total": late_total})
    await recorder.finish(invocation, "completed", token_usage={"total": 4})
    assert (await ledger.get_scope("scope")).spent_tokens == 4
    assert len(sync_db_session.scalars(select(UsageEvent)).all()) == 1


@pytest.mark.asyncio
async def test_unknown_partial_usage_accepts_larger_late_total(sync_db_session: Session) -> None:
    from apflow.core.storage.sqlalchemy.models import TaskModel
    from apflow.governance.ledger import BudgetLedger

    sync_db_session.add(TaskModel(id="late", name="Late"))
    sync_db_session.commit()
    recorder = AttemptRecorder(sync_db_session)
    invocation = await recorder.start("late", 0, 1)
    ledger = BudgetLedger(sync_db_session)
    await ledger.configure_scope("scope", 100)
    await ledger.reserve("scope", "late", invocation, 20)
    await recorder.finish(invocation, "unknown", token_usage={"total": 2})
    await recorder.finish(invocation, "completed", token_usage={"total": 7})
    await recorder.finish(invocation, "completed", token_usage={"total": 7})
    assert (await ledger.get_scope("scope")).spent_tokens == 7


@pytest.mark.asyncio
async def test_partial_receipts_cannot_change_finalized_usage(sync_db_session: Session) -> None:
    from sqlalchemy import select
    from apflow.core.storage.sqlalchemy.models import TaskModel, UsageEvent
    from apflow.governance.ledger import BudgetLedger

    sync_db_session.add(TaskModel(id="late", name="Late"))
    sync_db_session.commit()
    recorder = AttemptRecorder(sync_db_session)
    invocation = await recorder.start("late", 0, 1)
    ledger = BudgetLedger(sync_db_session)
    await ledger.configure_scope("scope", 100)
    await ledger.reserve("scope", "late", invocation, 20)
    await recorder.record_partial("late", 0, {"total": 2})
    await recorder.finish(invocation, "cancelled", token_usage={"total": 5})
    await recorder.record_partial("late", 0, {"total": 2})
    await recorder.record_partial("late", 0, {"total": 4})
    with pytest.raises(ValueError, match="Terminal invocations"):
        await recorder.record_partial("late", 0, {"total": 8})
    assert (await ledger.get_scope("scope")).spent_tokens == 5
    task = sync_db_session.get(TaskModel, "late")
    assert task is not None and task.token_usage is not None and task.token_usage["total"] == 5
    assert len(sync_db_session.scalars(select(UsageEvent)).all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("late_receipt", [False, True])
async def test_original_final_duplicate_preserves_later_correction(
    sync_db_session: Session, late_receipt: bool
) -> None:
    from sqlalchemy import select
    from apflow.core.storage.sqlalchemy.models import TaskModel, UsageEvent
    from apflow.governance.ledger import BudgetLedger
    from apflow.governance.usage import UsageRecorder

    sync_db_session.add(TaskModel(id="late", name="Late"))
    sync_db_session.commit()
    recorder = AttemptRecorder(sync_db_session)
    invocation = await recorder.start("late", 0, 1)
    ledger = BudgetLedger(sync_db_session)
    await ledger.configure_scope("scope", 100)
    await ledger.reserve("scope", "late", invocation, 20)
    await recorder.record_partial("late", 0, {"total": 5})
    if late_receipt:
        await recorder.finish(invocation, "unknown")
    await recorder.finish(invocation, "completed", token_usage={"total": 5})
    await UsageRecorder(sync_db_session).reconcile(
        invocation,
        "provider-corrected",
        {"total": 3},
        {
            "evidence_ref": "provider:corrected",
            "actor": "operator",
            "observed_at": "2026-10-24T00:00:00+00:00",
        },
    )
    await recorder.record_partial("late", 0, {"total": 5})
    await recorder.finish(invocation, "completed", token_usage={"total": 5})
    with pytest.raises(ValueError, match="different final usage"):
        await recorder.finish(invocation, "completed", token_usage={"total": 8})
    assert (await ledger.get_scope("scope")).spent_tokens == 3
    assert len(sync_db_session.scalars(select(UsageEvent)).all()) == 3


@pytest.mark.asyncio
async def test_late_worker_refreshes_invocation_recovered_in_another_session(
    sync_db_session: Session,
) -> None:
    from sqlalchemy import select, update
    from apflow.core.storage.sqlalchemy.models import ExecutionAttempt, TaskModel, UsageEvent
    from apflow.governance.ledger import BudgetLedger

    sync_db_session.add(TaskModel(id="late", name="Late"))
    sync_db_session.commit()
    engine = sync_db_session.get_bind()
    with Session(engine, expire_on_commit=False) as worker:
        recorder = AttemptRecorder(worker)
        invocation = await recorder.start("late", 0, 1)
        cached = worker.get(ExecutionAttempt, invocation)
        assert cached is not None and cached.status == "running"
        ledger = BudgetLedger(worker)
        await ledger.configure_scope("scope", 100)
        await ledger.reserve("scope", "late", invocation, 20)
        with Session(engine) as recovery:
            recovery.execute(
                update(ExecutionAttempt)
                .where(ExecutionAttempt.id == invocation)
                .values(status="unknown")
            )
            await BudgetLedger(recovery).settle(invocation, None)
        await recorder.finish(invocation, "completed", token_usage={"total": 7})
        worker.expire_all()
        assert cached.status == "unknown" and cached.token_usage is None
        assert worker.scalars(select(UsageEvent.id)).one() == invocation + ":late-final"
        balance = await ledger.get_scope("scope")
        assert balance.spent_tokens == 7 and balance.unknown_tokens == 0


@pytest.mark.asyncio
async def test_start_preserves_latest_legacy_usage_from_another_session(
    sync_db_session: Session,
) -> None:
    from sqlalchemy import select, update
    from apflow.core.storage.sqlalchemy.models import TaskModel, UsageEvent

    sync_db_session.add(TaskModel(id="legacy", name="Legacy", token_usage={"total": 1}))
    sync_db_session.commit()
    engine = sync_db_session.get_bind()
    with Session(engine, expire_on_commit=False) as worker:
        task = worker.get(TaskModel, "legacy")
        assert task is not None and task.token_usage == {"total": 1}
        with Session(engine) as legacy:
            legacy.execute(
                update(TaskModel).where(TaskModel.id == "legacy").values(token_usage={"total": 4})
            )
            legacy.commit()
        await AttemptRecorder(worker).start("legacy", 0, 1)
        baseline = worker.scalars(select(UsageEvent).where(UsageEvent.kind == "legacy")).one()
        assert baseline.token_usage["total"] == 4
