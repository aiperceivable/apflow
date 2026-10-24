"""Consumption facts are idempotent, immutable, and atomic with projections."""

from collections.abc import Iterator
from importlib import import_module
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Column, MetaData, Table, create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from apflow.core.storage.sqlalchemy.models import (
    Base,
    TaskModel,
    UsageEvent,
    WorkflowRun,
    ExecutionAttempt,
)
from apflow.durability.attempts import AttemptRecorder
from apflow.governance.usage import UsageRecorder
from apflow.governance.ledger import BudgetLedger
from apflow.governance.reporter import UsageReporter
from apflow.governance.budget import BudgetManager
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.core.execution.errors import ValidationError


@pytest.fixture
def usage_session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(TaskModel(id="task", name="task", user_id="owner", run_id="run"))
        session.commit()
        yield session
    engine.dispose()


@pytest.mark.asyncio
async def test_usage_events_deduplicate_and_conflicts_reject(usage_session: Session) -> None:
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    usage = UsageRecorder(usage_session)
    await usage.record(invocation, "provider-event", {"total": 5})
    await usage.record(invocation, "provider-event", {"total": 5})
    with pytest.raises(ValueError, match="different"):
        await usage.record(invocation, "provider-event", {"total": 6})
    usage_session.expire_all()
    task = usage_session.get(TaskModel, "task")
    assert task is not None and task.token_usage["total"] == 5
    assert len(usage_session.scalars(select(UsageEvent)).all()) == 1


def test_run_model_preserves_direct_lineage() -> None:
    assert {"run_id", "source_task_id", "result_snapshot_hash"} <= set(
        TaskModel.__table__.columns.keys()
    )
    assert "retry_of_run_id" in WorkflowRun.__table__.columns


def test_run_definition_is_immutable_but_lifecycle_can_change(usage_session: Session) -> None:
    run = WorkflowRun(
        id="run", root_task_id="task", definition_snapshot={"tasks": []}, definition_hash="hash"
    )
    usage_session.add(run)
    usage_session.commit()
    run.status = "running"
    usage_session.commit()
    run.definition_snapshot = {"tasks": ["replacement"]}
    with pytest.raises(ValidationError, match="immutable"):
        usage_session.commit()
    usage_session.rollback()


@pytest.mark.asyncio
async def test_partial_snapshot_repeated_and_final_usage_not_double_counted(
    usage_session: Session,
) -> None:
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    await recorder.record_partial("task", 0, {"total": 2})
    await recorder.record_partial("task", 0, {"total": 2})
    await recorder.finish(invocation, "cancelled", token_usage={"total": 5})
    usage_session.expire_all()
    task = usage_session.get(TaskModel, "task")
    assert task is not None and task.token_usage["total"] == 5
    assert [
        event.token_usage["total"]
        for event in usage_session.scalars(select(UsageEvent).order_by(UsageEvent.index))
    ] == [2, 3]


@pytest.mark.asyncio
async def test_finalization_rolls_back_every_accounting_write(
    usage_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    ledger = BudgetLedger(usage_session)
    await ledger.configure_scope("scope", 100)
    await ledger.reserve("scope", "task", invocation, 10)

    async def fail_commit() -> None:
        raise RuntimeError("Injected finalization interruption")

    monkeypatch.setattr(recorder, "_commit", fail_commit)
    with pytest.raises(RuntimeError, match="interruption"):
        await recorder.finish(invocation, "completed", token_usage={"total": 5})
    usage_session.expire_all()
    task = usage_session.get(TaskModel, "task")
    attempt = usage_session.get(ExecutionAttempt, invocation)
    assert task is not None and task.token_usage is None
    assert attempt is not None and attempt.status == "running"
    assert usage_session.scalars(select(UsageEvent)).all() == []
    balance = await ledger.get_scope("scope")
    assert balance.reserved_tokens == 10 and balance.spent_tokens == 0


@pytest.mark.asyncio
async def test_late_old_generation_consumption_is_not_lost(usage_session: Session) -> None:
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    task = usage_session.get(TaskModel, "task")
    assert task is not None
    task.attempt_id = 9
    task.status = "completed"
    task.result = "new result"
    usage_session.commit()
    await recorder.finish(invocation, "failed", token_usage={"total": 7})
    usage_session.expire_all()
    task = usage_session.get(TaskModel, "task")
    assert task is not None and task.token_usage["total"] == 7
    assert task.result == "new result" and task.attempt_id == 9


@pytest.mark.asyncio
async def test_outbox_cursor_and_ack_are_owner_scoped(usage_session: Session) -> None:
    invocation = await AttemptRecorder(usage_session).start("task", 0, 1)
    usage = UsageRecorder(usage_session)
    await usage.record(invocation, "receipt-1", {"total": 1})
    events = await usage.list_events("owner")
    assert len(events) == 1 and events[0]["delivered_at"] is None
    assert await usage.list_events("other") == []
    assert len(await usage.list_events("owner", events[0]["index"])) == 1
    with pytest.raises(KeyError):
        await usage.acknowledge("receipt-1", "other")
    await usage.acknowledge("receipt-1", "owner")
    assert await usage.list_events("owner", events[0]["index"]) == []
    first = (await usage.list_events("owner"))[0]
    await usage.acknowledge("receipt-1", "owner")
    assert (await usage.list_events("owner"))[0]["delivered_at"] == first["delivered_at"]


@pytest.mark.asyncio
async def test_empty_final_usage_keeps_reservation_unknown(usage_session: Session) -> None:
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    ledger = BudgetLedger(usage_session)
    await ledger.configure_scope("scope", 100)
    await ledger.reserve("scope", "task", invocation, 10)
    await recorder.finish(invocation, "completed", token_usage={})
    assert (await ledger.get_scope("scope")).unknown_tokens == 10


@pytest.mark.asyncio
async def test_report_uses_events_even_if_projection_is_corrupted(usage_session: Session) -> None:
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    await recorder.finish(invocation, "completed", token_usage={"total": 5})
    task = usage_session.get(TaskModel, "task")
    assert task is not None
    task.token_usage = {"total": 999}
    usage_session.commit()
    assert (
        await UsageReporter(TaskRepository(usage_session)).get_user_usage("owner")
    ).total_tokens == 5


@pytest.mark.asyncio
async def test_link_and_unexecuted_copy_do_not_repeat_source_consumption(
    usage_session: Session,
) -> None:
    task = usage_session.get(TaskModel, "task")
    assert task is not None
    task.run_id = None
    task.token_usage = {"total": 10}
    task.actual_cost_usd = 1.5
    usage_session.add_all(
        [
            TaskModel(
                id=kind,
                name=kind,
                user_id="owner",
                origin_type=kind,
                original_task_id="task",
                token_usage={"total": 10},
                actual_cost_usd=1.5,
            )
            for kind in ("link", "copy")
        ]
    )
    usage_session.commit()
    report = await UsageReporter(TaskRepository(usage_session)).get_user_usage("owner")
    assert report.total_tokens == 10 and report.total_cost_usd == 1.5


@pytest.mark.asyncio
async def test_late_receipt_settles_unknown_and_preserves_attempt_history(
    usage_session: Session,
) -> None:
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    ledger = BudgetLedger(usage_session)
    await ledger.configure_scope("scope", 100)
    await ledger.reserve("scope", "task", invocation, 20)
    await recorder.finish(invocation, "unknown")
    evidence = {
        "evidence_ref": "provider:123",
        "actor": "owner",
        "observed_at": "2026-10-24T00:00:00+00:00",
    }
    usage = UsageRecorder(usage_session)
    await usage.reconcile(invocation, "receipt-123", {"total": 7}, evidence)
    await usage.reconcile(invocation, "receipt-123", {"total": 7}, evidence)
    with pytest.raises(ValueError, match="different"):
        await usage.reconcile(invocation, "receipt-123", {"total": 8}, evidence)
    usage_session.expire_all()
    attempt = usage_session.get(ExecutionAttempt, invocation)
    assert attempt is not None and attempt.status == "unknown" and attempt.token_usage is None
    balance = await ledger.get_scope("scope")
    assert balance.spent_tokens == 7 and balance.unknown_tokens == 0
    report = await UsageReporter(TaskRepository(usage_session)).get_task_usage("task")
    assert report.total_tokens == 7 and report.unknown_usage_task_count == 0
    events = await usage.list_events("owner")
    assert len(events) == 1 and events[0]["evidence"] == evidence


@pytest.mark.asyncio
async def test_evidenced_correction_appends_signed_delta(usage_session: Session) -> None:
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    ledger = BudgetLedger(usage_session)
    await ledger.configure_scope("scope", 100)
    await ledger.reserve("scope", "task", invocation, 20)
    await recorder.finish(invocation, "completed", token_usage={"total": 7})
    evidence = {
        "evidence_ref": "provider:correction",
        "actor": "owner",
        "observed_at": "2026-10-24T00:00:00+00:00",
    }
    usage = UsageRecorder(usage_session)
    await usage.reconcile(invocation, "corrected-receipt", {"total": 5}, evidence)
    await usage.reconcile(invocation, "corrected-receipt", {"total": 5}, evidence)
    events = await usage.list_events("owner")
    assert [event["token_usage"]["total"] for event in events] == [7, -2]
    assert (await ledger.get_scope("scope")).spent_tokens == 5
    usage_session.expire_all()
    task = usage_session.get(TaskModel, "task")
    assert task is not None and task.token_usage["total"] == 5


@pytest.mark.asyncio
async def test_repeated_corrections_refresh_cached_reservation(usage_session: Session) -> None:
    usage_session.expire_on_commit = False
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    ledger = BudgetLedger(usage_session)
    await ledger.configure_scope("scope", 100)
    await ledger.reserve("scope", "task", invocation, 20)
    await recorder.finish(invocation, "completed", token_usage={"total": 7})
    from apflow.core.storage.sqlalchemy.models import BudgetReservation

    cached = usage_session.get(BudgetReservation, invocation)
    assert cached is not None and cached.actual_tokens == 7
    usage = UsageRecorder(usage_session)
    evidence = {
        "evidence_ref": "provider:correction",
        "actor": "owner",
        "observed_at": "2026-10-24T00:00:00+00:00",
    }
    await usage.reconcile(invocation, "correction-5", {"total": 5}, evidence)
    await usage.reconcile(invocation, "correction-6", {"total": 6}, evidence)
    assert (await ledger.get_scope("scope")).spent_tokens == 6
    assert (await usage.total(invocation))["total"] == 6


@pytest.mark.asyncio
async def test_old_receipt_replay_cannot_reapply_superseded_usage(usage_session: Session) -> None:
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    ledger = BudgetLedger(usage_session)
    await ledger.configure_scope("scope", 100)
    await ledger.reserve("scope", "task", invocation, 20)
    await recorder.finish(invocation, "completed", token_usage={"total": 7})
    usage = UsageRecorder(usage_session)
    evidence = {
        "evidence_ref": "provider:correction",
        "actor": "owner",
        "observed_at": "2026-10-24T00:00:00+00:00",
    }
    first = await usage.reconcile(invocation, "older-receipt", {"total": 5}, evidence)
    await usage.reconcile(invocation, "newer-receipt", {"total": 9}, evidence)
    assert await usage.reconcile(invocation, "older-receipt", {"total": 5}, evidence) == first
    assert (await ledger.get_scope("scope")).spent_tokens == 9
    assert (await usage.total(invocation))["total"] == 9
    assert len(await usage.list_events("owner")) == 3
    usage_session.expire_all()
    attempt = usage_session.get(ExecutionAttempt, invocation)
    assert attempt is not None and attempt.status == "completed"
    assert attempt.token_usage == {"total": 7}


@pytest.mark.asyncio
async def test_correction_audit_failure_rolls_back_usage_and_budget(
    usage_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    ledger = BudgetLedger(usage_session)
    await ledger.configure_scope("scope", 100)
    await ledger.reserve("scope", "task", invocation, 20)
    await recorder.finish(invocation, "completed", token_usage={"total": 7})
    evidence = {
        "evidence_ref": "provider:correction",
        "actor": "owner",
        "observed_at": "2026-10-24T00:00:00+00:00",
    }

    async def fail_audit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Audit store unavailable")

    monkeypatch.setattr(BudgetLedger, "_record_reconciliation", fail_audit)
    usage = UsageRecorder(usage_session)
    with pytest.raises(RuntimeError, match="Audit store"):
        await usage.reconcile(invocation, "failed-correction", {"total": 5}, evidence)
    usage_session.expire_all()
    task = usage_session.get(TaskModel, "task")
    assert task is not None and task.token_usage["total"] == 7
    assert (await ledger.get_scope("scope")).spent_tokens == 7
    assert (await usage.total(invocation))["total"] == 7
    assert len(await usage.list_events("owner")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("evidenced", [False, True])
async def test_terminal_new_usage_requires_atomic_reconciliation(
    usage_session: Session, evidenced: bool
) -> None:
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    ledger = BudgetLedger(usage_session)
    await ledger.configure_scope("scope", 100)
    await ledger.reserve("scope", "task", invocation, 20)
    await recorder.finish(invocation, "completed", token_usage={"total": 5})
    evidence = (
        {
            "evidence_ref": "provider:receipt",
            "actor": "owner",
            "observed_at": "2026-10-24T00:00:00+00:00",
        }
        if evidenced
        else None
    )
    usage = UsageRecorder(usage_session)
    with pytest.raises(ValueError, match="Terminal.*reconciliation"):
        await usage.record(invocation, "unsettled-late-delta", {"total": 3}, evidence=evidence)
    assert (await ledger.get_scope("scope")).spent_tokens == 5
    assert (await usage.total(invocation))["total"] == 5
    usage_session.expire_all()
    task = usage_session.get(TaskModel, "task")
    assert task is not None and task.token_usage["total"] == 5


@pytest.mark.asyncio
async def test_cancelled_usage_append_cannot_leak_on_later_commit(
    usage_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation = await AttemptRecorder(usage_session).start("task", 0, 1)

    async def interrupted_projection(*args: object, **kwargs: object) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(TaskRepository, "increment_token_usage", interrupted_projection)
    with pytest.raises(asyncio.CancelledError):
        await UsageRecorder(usage_session).record(invocation, "cancelled-report", {"total": 5})
    usage_session.commit()
    usage_session.expire_all()
    assert usage_session.scalars(select(UsageEvent)).all() == []
    task = usage_session.get(TaskModel, "task")
    assert task is not None and task.token_usage is None


@pytest.mark.asyncio
async def test_consumption_facts_reject_orm_rewrites(usage_session: Session) -> None:
    invocation = await AttemptRecorder(usage_session).start("task", 0, 1)
    await UsageRecorder(usage_session).record(invocation, "immutable", {"total": 1})
    event = usage_session.scalar(select(UsageEvent))
    assert event is not None
    event.token_usage = {"total": 99}
    with pytest.raises(ValueError, match="immutable"):
        usage_session.commit()
    usage_session.rollback()
    event = usage_session.scalar(select(UsageEvent))
    assert event is not None
    usage_session.delete(event)
    with pytest.raises(ValueError, match="deleted"):
        usage_session.commit()
    usage_session.rollback()


def test_migration_preserves_006_usage_and_repeated_upgrade_is_idempotent() -> None:
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for model in (TaskModel, ExecutionAttempt):
        Table(
            model.__tablename__,
            metadata,
            *[
                Column(
                    column.name,
                    column.type,
                    primary_key=column.primary_key,
                    nullable=column.nullable,
                    server_default=column.server_default,
                )
                for column in model.__table__.columns
                if column.name not in {"run_id", "source_task_id", "result_snapshot_hash"}
            ],
        )
    metadata.create_all(engine)
    with engine.begin() as connection:
        for task_id, origin in (("source", "create"), ("copy", "copy"), ("link", "link")):
            connection.execute(
                metadata.tables[TaskModel.__tablename__]
                .insert()
                .values(
                    id=task_id,
                    name=task_id,
                    origin_type=origin,
                    user_id="owner",
                    token_usage={"total": 7},
                )
            )
    migration = import_module(
        "apflow.core.storage.migrations.007_add_workflow_runs_usage_events"
    ).AddWorkflowRunsUsageEvents()
    migration.upgrade(engine)
    migration.upgrade(engine)
    assert {"run_id", "source_task_id", "result_snapshot_hash"} <= {
        column["name"] for column in inspect(engine).get_columns(TaskModel.__tablename__)
    }
    with Session(engine) as session:
        rows = session.scalars(select(UsageEvent)).all()
        assert (
            len(rows) == 1 and rows[0].task_id == "source" and rows[0].token_usage == {"total": 7}
        )
        assert session.get(TaskModel, "copy") is not None
        assert session.get(TaskModel, "link") is not None
    engine.dispose()


def test_racing_duplicate_events_charge_once(tmp_path: Path) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent-usage.db'}", connect_args={"timeout": 10}
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(TaskModel(id="task", name="task", user_id="owner"))
        session.commit()
        invocation = asyncio.run(AttemptRecorder(session).start("task", 0, 1))

    def record(_: int) -> None:
        with Session(engine) as session:
            asyncio.run(UsageRecorder(session).record(invocation, "same-receipt", {"total": 3}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(record, [1, 2]))
    with Session(engine) as session:
        task = session.get(TaskModel, "task")
        assert task is not None and task.token_usage["total"] == 3
        assert len(session.scalars(select(UsageEvent)).all()) == 1
    engine.dispose()


def test_legacy_usage_and_first_invocation_preserve_accounting_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-boundary.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(TaskModel(id="task", name="task", user_id="owner", token_usage={"total": 100}))
        session.commit()
    checked = Event()
    started = Event()
    original_check = BudgetManager._require_legacy_usage

    async def pause_after_check(self: BudgetManager, task_id: str) -> None:
        await original_check(self, task_id)
        checked.set()
        started.wait(timeout=1)

    monkeypatch.setattr(BudgetManager, "_require_legacy_usage", pause_after_check)

    def update_legacy() -> None:
        with Session(engine) as session:
            asyncio.run(BudgetManager(TaskRepository(session)).update_usage("task", {"total": 3}))

    def invoke() -> None:
        assert checked.wait(timeout=5)
        with Session(engine) as session:
            recorder = AttemptRecorder(session)
            invocation = asyncio.run(recorder.start("task", 0, 1))
            started.set()
            asyncio.run(recorder.finish(invocation, "completed", token_usage={"total": 5}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(update_legacy), pool.submit(invoke)]
        for future in futures:
            future.result()
    with Session(engine) as session:
        report = asyncio.run(UsageReporter(TaskRepository(session)).get_task_usage("task"))
        task = session.get(TaskModel, "task")
        assert task is not None and task.token_usage["total"] == 108
        assert report.total_tokens == 108
    engine.dispose()


@pytest.mark.asyncio
async def test_async_expiring_session_returns_durable_event_without_lazy_io() -> None:
    pytest.importorskip("aiosqlite")
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=True) as session:
        session.add(TaskModel(id="task", name="task", user_id="owner"))
        await session.commit()
        invocation = await AttemptRecorder(session).start("task", 0, 1)
        event = await UsageRecorder(session).record(invocation, "async-event", {"total": 3})
        assert event["id"] == "async-event" and event["token_usage"]["total"] == 3
    await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_baseline_is_preserved_but_not_delivered_as_new_charge(
    usage_session: Session,
) -> None:
    task = usage_session.get(TaskModel, "task")
    assert task is not None
    task.token_usage = {"total": 10}
    usage_session.commit()
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    await recorder.finish(invocation, "completed", token_usage={"total": 3})
    events = await UsageRecorder(usage_session).list_events("owner")
    assert len(events) == 1 and events[0]["token_usage"]["total"] == 3
    assert (
        await UsageReporter(TaskRepository(usage_session)).get_task_usage("task")
    ).total_tokens == 13


@pytest.mark.asyncio
async def test_expired_invocation_late_final_usage_is_accounted_once(
    usage_session: Session,
) -> None:
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    ledger = BudgetLedger(usage_session)
    await ledger.configure_scope("scope", 100)
    await ledger.reserve("scope", "task", invocation, 20)
    attempt = usage_session.get(ExecutionAttempt, invocation)
    assert attempt is not None
    attempt.status = "unknown"
    usage_session.commit()
    await recorder.finish(invocation, "completed", token_usage={"total": 7})
    await recorder.finish(invocation, "completed", token_usage={"total": 7})
    usage_session.expire_all()
    attempt = usage_session.get(ExecutionAttempt, invocation)
    assert attempt is not None and attempt.status == "unknown"
    assert (await ledger.get_scope("scope")).spent_tokens == 7
    events = await UsageRecorder(usage_session).list_events("owner")
    assert len(events) == 1 and events[0]["token_usage"]["total"] == 7


@pytest.mark.asyncio
async def test_period_uses_consumption_event_time_not_task_creation(usage_session: Session) -> None:
    task = usage_session.get(TaskModel, "task")
    assert task is not None
    task.created_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    task.token_usage = {"total": 10}
    usage_session.commit()
    recorder = AttemptRecorder(usage_session)
    invocation = await recorder.start("task", 0, 1)
    await recorder.finish(invocation, "completed", token_usage={"total": 5})
    event = usage_session.scalar(select(UsageEvent).where(UsageEvent.kind != "legacy"))
    assert event is not None
    start, end = event.created_at - timedelta(seconds=1), event.created_at + timedelta(seconds=1)
    report = await UsageReporter(TaskRepository(usage_session)).get_user_usage("owner", start, end)
    assert report.total_tokens == 5 and report.task_count == 1


def test_concurrent_distinct_corrections_keep_usage_and_budget_aligned(tmp_path: Path) -> None:
    from threading import Barrier
    from apflow.core.storage.sqlalchemy.models import BudgetReservation

    engine = create_engine(
        f"sqlite:///{tmp_path / 'distinct-corrections.db'}", connect_args={"timeout": 10}
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(TaskModel(id="task", name="task", user_id="owner"))
        session.commit()
        recorder = AttemptRecorder(session)
        invocation = asyncio.run(recorder.start("task", 0, 1))
        ledger = BudgetLedger(session)
        asyncio.run(ledger.configure_scope("scope", 100))
        asyncio.run(ledger.reserve("scope", "task", invocation, 20))
        asyncio.run(recorder.finish(invocation, "completed", token_usage={"total": 7}))
    both_cached = Barrier(2)

    def correct(total: int) -> None:
        with Session(engine, expire_on_commit=False) as session:
            cached = session.get(BudgetReservation, invocation)
            assert cached is not None and cached.actual_tokens == 7
            both_cached.wait(timeout=5)
            asyncio.run(
                UsageRecorder(session).reconcile(
                    invocation,
                    f"correction-{total}",
                    {"total": total},
                    {
                        "evidence_ref": f"provider:correction-{total}",
                        "actor": "operator",
                        "observed_at": "2026-10-24T00:00:00+00:00",
                    },
                )
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(correct, [5, 9]))
        with Session(engine) as session:
            events = session.scalars(
                select(UsageEvent)
                .where(UsageEvent.invocation_id == invocation)
                .order_by(UsageEvent.index)
            ).all()
            assert len(events) == 3
            assert events[0].token_usage["total"] == 7
            assert {event.id for event in events[1:]} == {"correction-5", "correction-9"}
            total = sum(event.token_usage["total"] for event in events)
            assert total == events[-1].reported_usage["total"]
            assert total in {5, 9}
            task = session.get(TaskModel, "task")
            attempt = session.get(ExecutionAttempt, invocation)
            assert task is not None and task.token_usage["total"] == total
            assert attempt is not None and attempt.token_usage == {"total": 7}
            assert attempt.status == "completed"
            balance = asyncio.run(BudgetLedger(session).get_scope("scope"))
            assert balance.spent_tokens == total
            assert balance.reserved_tokens == balance.unknown_tokens == 0
    finally:
        engine.dispose()
