"""PostgreSQL acceptance checks using disposable databases on an explicit test server."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import asyncio
import importlib
from threading import Barrier
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    Table,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateColumn

from apflow.core.execution.run_retry import retry_run
from apflow.core.execution.runs import RunService
from apflow.core.execution.task_manager import TaskManager
from apflow.core.storage.sqlalchemy.models import (
    TASK_TABLE_NAME,
    BudgetReservation,
    ExecutionAttempt,
    TaskCheckpointModel,
    TaskModel,
    UsageEvent,
    WorkflowRun,
)
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.durability.attempts import AttemptRecorder
from apflow.durability.checkpoint import CheckpointManager
from apflow.governance.ledger import BudgetLedger
from apflow.governance.usage import UsageRecorder
from tests.integration.postgres_helpers import postgres_session

pytestmark = pytest.mark.integration


def test_postgres_migration_004_upgrades_legacy_schema(postgres_database_url: str) -> None:
    migration_module = importlib.import_module(
        "apflow.core.storage.migrations.004_add_durability_and_governance"
    )
    migration = migration_module.AddDurabilityAndGovernance()
    new_columns = set(migration_module.DURABILITY_COLUMNS) | set(
        migration_module.GOVERNANCE_COLUMNS
    )
    engine = create_engine(postgres_database_url)
    try:
        definitions = [
            str(CreateColumn(column).compile(dialect=engine.dialect))
            for column in TaskModel.__mapper__.columns
            if column.name not in new_columns
        ]
        definitions.append("PRIMARY KEY (id)")
        with engine.begin() as connection:
            connection.execute(text(f"CREATE TABLE {TASK_TABLE_NAME} ({', '.join(definitions)})"))
            connection.execute(
                text(f"INSERT INTO {TASK_TABLE_NAME} (id, name) VALUES ('legacy', 'Legacy task')")
            )
        legacy_columns = {column["name"] for column in inspect(engine).get_columns(TASK_TABLE_NAME)}
        assert new_columns.isdisjoint(legacy_columns)

        migration.upgrade(engine)
        migration.upgrade(engine)

        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns(TASK_TABLE_NAME)}
        assert set(columns) == legacy_columns | new_columns
        checkpoint_type = columns["checkpoint_at"]["type"]
        assert isinstance(checkpoint_type, DateTime)
        assert checkpoint_type.timezone is True
        assert {column["name"] for column in inspector.get_columns("task_checkpoints")} == {
            "id",
            "task_id",
            "checkpoint_data",
            "step_name",
            "created_at",
        }
        checkpoint_indexes = {index["name"] for index in inspector.get_indexes("task_checkpoints")}
        assert {
            "idx_task_checkpoints_task_id",
            "idx_task_checkpoints_created_at",
        } <= checkpoint_indexes
        with Session(engine) as session:
            task = session.get(TaskModel, "legacy")
            assert task is not None
            assert task.name == "Legacy task"
            assert task.attempt_count == 0
            assert task.max_attempts == 3
            assert task.backoff_strategy == "exponential"
            assert task.backoff_base_seconds == Decimal("1.00")
            assert task.token_usage is None
            assert task.token_budget is None
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_postgres_checkpoint_survives_session_and_engine_reopen(
    postgres_database_url: str,
) -> None:
    checkpoint_data = {"position": 7, "completed": ["scan", "transform"], "nested": {"value": None}}
    with postgres_session(postgres_database_url) as session:
        task = await TaskRepository(session).create_task("Checkpointed task")
        task_id = task.id
        checkpoint_id = await CheckpointManager(session).save_checkpoint(
            task_id, checkpoint_data, step_name="transform"
        )

    with postgres_session(postgres_database_url) as reopened_session:
        assert await CheckpointManager(reopened_session).load_checkpoint(task_id) == checkpoint_data
        restored_task = reopened_session.get(TaskModel, task_id)
        assert restored_task is not None
        assert restored_task.resume_from == checkpoint_id
        assert restored_task.checkpoint_at is not None
        assert restored_task.checkpoint_at.tzinfo is not None
        checkpoint = reopened_session.scalar(
            select(TaskCheckpointModel).where(TaskCheckpointModel.id == checkpoint_id)
        )
        assert checkpoint is not None
        assert checkpoint.step_name == "transform"
        assert checkpoint.task_id == task_id


@pytest.mark.asyncio
async def test_postgres_factory_and_all_governance_fields_survive_reload(
    postgres_database_url: str,
) -> None:
    with postgres_session(postgres_database_url) as session:
        repository = TaskRepository(session)
        task = await repository.create_task(
            "Governed task",
            token_usage={"input": 10, "output": 5, "total": 15},
            token_budget=1000,
            estimated_cost_usd=Decimal("0.125000"),
            actual_cost_usd=Decimal("0.062500"),
            cost_policy="notify-at-eighty",
        )
        task_id = task.id
        updated = await repository.increment_token_usage(task_id, 7, 3, 10)
        assert updated is task
        assert task.token_usage == {"input": 17, "output": 8, "total": 25}
        assert (
            session.scalar(text("SELECT current_database()"))
            == make_url(postgres_database_url).database
        )

    with postgres_session(postgres_database_url) as reopened_session:
        restored = reopened_session.get(TaskModel, task_id)
        assert restored is not None
        assert restored.name == "Governed task"
        assert restored.token_usage == {"input": 17, "output": 8, "total": 25}
        assert restored.token_budget == 1000
        assert restored.estimated_cost_usd == Decimal("0.125000")
        assert restored.actual_cost_usd == Decimal("0.062500")
        assert restored.cost_policy == "notify-at-eighty"


def legacy_run_metadata() -> MetaData:
    """Represent the task and invocation tables before migration 007."""
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
    return metadata


def test_postgres_migration_007_preserves_legacy_usage_and_event_sequence(
    postgres_database_url: str,
) -> None:
    engine = create_engine(postgres_database_url)
    metadata = legacy_run_metadata()
    try:
        metadata.create_all(engine)
        with engine.begin() as connection:
            for identifier, origin in (("source", "create"), ("copy", "copy"), ("link", "link")):
                connection.execute(
                    metadata.tables[TaskModel.__tablename__]
                    .insert()
                    .values(
                        id=identifier,
                        name=identifier,
                        origin_type=origin,
                        user_id="alice",
                        token_usage={"total": 7},
                    )
                )
        migration = importlib.import_module(
            "apflow.core.storage.migrations.007_add_workflow_runs_usage_events"
        ).AddWorkflowRunsUsageEvents()
        migration.upgrade(engine)
        migration.upgrade(engine)
        with Session(engine) as session:
            baseline = session.scalars(select(UsageEvent)).all()
            assert len(baseline) == 1
            assert baseline[0].task_id == "source" and baseline[0].token_usage == {"total": 7}
            session.add(
                UsageEvent(
                    id="new-receipt",
                    invocation_id="new-invocation",
                    task_id="new-task",
                    owner_id="alice",
                    kind="delta",
                    digest="test",
                    reported_usage={"total": 3},
                    token_usage={"total": 3},
                )
            )
            session.commit()
            events = session.scalars(select(UsageEvent).order_by(UsageEvent.index)).all()
            assert events[1].index > events[0].index > 0
            original = session.get(TaskModel, "source")
            assert original is not None and original.token_usage == {"total": 7}
        assert {"run_id", "source_task_id", "result_snapshot_hash"} <= {
            column["name"] for column in inspect(engine).get_columns(TASK_TABLE_NAME)
        }
    finally:
        engine.dispose()


async def cancel_run_with_completed_dependency(
    session: Session,
) -> tuple[TaskModel, TaskModel, str]:
    """Build a cancelled run with reusable output and an unresolved final receipt."""
    repository = TaskRepository(session)
    root = await repository.create_task("B", user_id="alice", budget_scope_id="scope")
    completed = await repository.create_task("A", parent_id=root.id, user_id="alice")
    await repository.update_task(root.id, dependencies=[completed.id])
    runs = RunService(session)
    await runs.start_run(root.id)
    recorder = AttemptRecorder(session)
    ledger = BudgetLedger(session)
    await ledger.configure_scope("scope", 100)
    assert await repository.try_claim_for_execution(completed.id) is not None
    first_invocation = await recorder.start(completed.id, 1, 1)
    await ledger.reserve("scope", completed.id, first_invocation, 10)
    await recorder.finish(first_invocation, "completed", token_usage={"total": 3})
    await repository.transition_task(
        completed.id, expected_statuses=["in_progress"], status="completed", result="A result"
    )
    assert await repository.try_claim_for_execution(root.id) is not None
    old_invocation = await recorder.start(root.id, root.attempt_id, 1)
    await ledger.reserve("scope", root.id, old_invocation, 10)
    executor = AsyncMock()
    executor.cancel.return_value = {
        "status": "cancelled",
        "partial_result": "B partial",
        "token_usage": {"total": 2},
    }
    manager = TaskManager(session, executor_instances={root.id: executor})
    assert (await manager.cancel_task(root.id))["stop_confirmed"] is True
    await manager.cancel_task(root.id)
    assert executor.cancel.await_count == 1
    await recorder.finish(old_invocation, "cancelled")
    assert (await ledger.get_scope("scope")).unknown_tokens == 10
    assert await runs.finish_from_tasks(root.id) == "cancelled"
    return root, completed, old_invocation


@pytest.mark.asyncio
async def test_postgres_retry_cancel_and_late_receipt_preserve_original_history(
    postgres_database_url: str,
) -> None:
    with postgres_session(postgres_database_url) as session:
        root, completed, old_invocation = await cancel_run_with_completed_dependency(session)
        repository = TaskRepository(session)
        runs, recorder, ledger = (
            RunService(session),
            AttemptRecorder(session),
            BudgetLedger(session),
        )
        old_run = session.get(WorkflowRun, root.id)
        assert old_run is not None
        run_history = old_run.to_dict()
        task_history = (root.status, root.result, root.completed_at, root.attempt_id)
        receipt = await retry_run(repository, root.id, "retry-B", "alice")
        assert await retry_run(repository, root.id, "retry-B", "alice") == receipt
        new_root_id = receipt["root_task_id"]
        await runs.start_run(new_root_id)
        assert await repository.try_claim_for_execution(new_root_id) is not None
        new_invocation = await recorder.start(new_root_id, 1, 1)
        await ledger.reserve("scope", new_root_id, new_invocation, 10)
        await recorder.finish(new_invocation, "completed", token_usage={"total": 5})
        await repository.transition_task(
            new_root_id, expected_statuses=["in_progress"], status="completed", result="B result"
        )
        assert await runs.finish_from_tasks(new_root_id) == "completed"
        await recorder.finish(old_invocation, "completed", token_usage={"total": 7})
        await recorder.finish(old_invocation, "completed", token_usage={"total": 7})
        session.refresh(old_run)
        session.refresh(root)
        assert old_run.to_dict() == run_history
        assert (root.status, root.result, root.completed_at, root.attempt_id) == task_history
        assert root.token_usage["total"] == 7
        reused = session.get(TaskModel, receipt["task_mapping"][completed.id])
        assert reused is not None and reused.result == "A result" and reused.token_usage is None
        events = await UsageRecorder(session).list_events("alice")
        assert sum(e["token_usage"]["total"] for e in events if e["run_id"] == root.id) == 10
        assert sum(e["token_usage"]["total"] for e in events if e["run_id"] == new_root_id) == 5
        balance = await ledger.get_scope("scope")
        assert balance.spent_tokens == 15
        assert balance.reserved_tokens == balance.unknown_tokens == 0
        original_attempt = session.get(ExecutionAttempt, old_invocation)
        assert original_attempt is not None and original_attempt.status == "cancelled"


@pytest.mark.asyncio
async def test_postgres_cancel_usage_failure_is_atomic_and_retryable(
    postgres_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with postgres_session(postgres_database_url) as session:
        repository = TaskRepository(session)
        task = await repository.create_task("Cancelling", status="in_progress", attempt_id=1)
        task_id = task.id
        invocation = await AttemptRecorder(session).start(task_id, 1, 1)
        executor = AsyncMock()
        executor.cancel.return_value = {"status": "cancelled", "token_usage": {"total": 2}}
        manager = TaskManager(session, executor_instances={task_id: executor})
        original = TaskRepository.increment_token_usage

        async def fail_projection(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Injected projection failure")

        monkeypatch.setattr(TaskRepository, "increment_token_usage", fail_projection)
        with pytest.raises(RuntimeError, match="projection failure"):
            await manager.cancel_task(task_id)
        session.refresh(task)
        assert task.status == "in_progress" and task.token_usage is None
        assert session.scalars(select(UsageEvent)).all() == []
        monkeypatch.setattr(TaskRepository, "increment_token_usage", original)
        assert (await manager.cancel_task(task_id))["stop_confirmed"] is True
        await AttemptRecorder(session).finish(invocation, "cancelled", token_usage={"total": 2})
        assert (await UsageRecorder(session).total(invocation))["total"] == 2


@pytest.mark.asyncio
async def test_postgres_outbox_redelivers_late_commit_below_acknowledged_cursor(
    postgres_database_url: str,
) -> None:
    with postgres_session(postgres_database_url) as session:
        repository = TaskRepository(session)
        first = await repository.create_task("First", user_id="alice")
        second = await repository.create_task("Second", user_id="alice")
        recorder = AttemptRecorder(session)
        first_invocation = await recorder.start(first.id, 1, 1)
        second_invocation = await recorder.start(second.id, 1, 1)
        engine = session.get_bind()
        with Session(engine) as late_session, Session(engine) as early_session:
            late = UsageRecorder(late_session)
            first_event = await late.record(
                first_invocation, "late-commit", {"total": 3}, commit=False
            )
            early = UsageRecorder(early_session)
            second_event = await early.record(second_invocation, "early-commit", {"total": 5})
            assert first_event["index"] < second_event["index"]
            visible = await early.list_events("alice")
            assert [event["id"] for event in visible] == ["early-commit"]
            await early.acknowledge("early-commit", "alice")
            late_session.commit()
            visible = await early.list_events("alice", second_event["index"])
            assert [event["id"] for event in visible] == ["late-commit"]
            await early.acknowledge("late-commit", "alice")
            await early.acknowledge("late-commit", "alice")
            assert await early.list_events("alice", second_event["index"]) == []


@pytest.mark.parametrize("reports", [(5, 5), (5, 9)])
def test_postgres_concurrent_corrections_keep_events_projection_and_budget_aligned(
    postgres_database_url: str, reports: tuple[int, int]
) -> None:
    with postgres_session(postgres_database_url) as session:
        repository = TaskRepository(session)
        task = asyncio.run(repository.create_task("Concurrent receipts", user_id="alice"))
        task_id = task.id
        recorder = AttemptRecorder(session)
        invocation = asyncio.run(recorder.start(task_id, 1, 1))
        ledger = BudgetLedger(session)
        asyncio.run(ledger.configure_scope("scope", 100))
        asyncio.run(ledger.reserve("scope", task_id, invocation, 20))
        asyncio.run(recorder.finish(invocation, "completed", token_usage={"total": 7}))
        engine = session.get_bind()
        ready = Barrier(2)

        def reconcile(total: int) -> None:
            with Session(engine, expire_on_commit=False) as worker:
                cached = worker.get(BudgetReservation, invocation)
                assert cached is not None and cached.actual_tokens == 7
                ready.wait(timeout=5)
                asyncio.run(
                    UsageRecorder(worker).reconcile(
                        invocation,
                        f"correction-{total}",
                        {"total": total},
                        {
                            "evidence_ref": f"provider:{total}",
                            "actor": "operator",
                            "observed_at": "2026-10-24T00:00:00+00:00",
                        },
                    )
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(reconcile, reports))
        session.expire_all()
        events = session.scalars(select(UsageEvent).order_by(UsageEvent.index)).all()
        assert len(events) == len(set(reports)) + 1
        total = sum(event.token_usage["total"] for event in events)
        assert total == events[-1].reported_usage["total"]
        restored = session.get(TaskModel, task_id)
        assert restored is not None and restored.token_usage["total"] == total
        assert asyncio.run(ledger.get_scope("scope")).spent_tokens == total
        attempt = session.get(ExecutionAttempt, invocation)
        assert attempt is not None and attempt.status == "completed"
        assert attempt.token_usage == {"total": 7}


@pytest.mark.parametrize("same_invocation", [False, True])
@pytest.mark.parametrize("initial_status", ["running", "unknown"])
def test_postgres_concurrent_final_reports_do_not_duplicate_or_lose_consumption(
    postgres_database_url: str, same_invocation: bool, initial_status: str
) -> None:
    with postgres_session(postgres_database_url) as session:
        task = asyncio.run(TaskRepository(session).create_task("Parallel reports", user_id="alice"))
        task_id = task.id
        recorder = AttemptRecorder(session)
        ledger = BudgetLedger(session)
        asyncio.run(ledger.configure_scope("scope", 100))
        invocations = [asyncio.run(recorder.start(task_id, 1, 1))]
        if not same_invocation:
            invocations.append(asyncio.run(recorder.start(task_id, 2, 1)))
        for invocation in invocations:
            asyncio.run(ledger.reserve("scope", task_id, invocation, 10))
            if initial_status == "unknown":
                asyncio.run(recorder.finish(invocation, "unknown"))
        engine = session.get_bind()
        ready = Barrier(2)

        def finish(invocation: str) -> None:
            with Session(engine) as worker:
                ready.wait(timeout=5)
                asyncio.run(
                    AttemptRecorder(worker).finish(
                        invocation, "completed", token_usage={"total": 5}
                    )
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(finish, [invocations[0], invocations[-1]]))
        session.expire_all()
        expected = 5 * len(invocations)
        events = session.scalars(select(UsageEvent)).all()
        assert len(events) == len(invocations)
        assert sum(event.token_usage["total"] for event in events) == expected
        restored = session.get(TaskModel, task_id)
        assert restored is not None and restored.token_usage["total"] == expected
        balance = asyncio.run(ledger.get_scope("scope"))
        assert balance.spent_tokens == expected
        assert balance.reserved_tokens == balance.unknown_tokens == 0
        attempts = session.scalars(select(ExecutionAttempt)).all()
        expected_status = "unknown" if initial_status == "unknown" else "completed"
        assert {attempt.status for attempt in attempts} == {expected_status}


def test_postgres_concurrent_retry_request_creates_only_one_new_run(
    postgres_database_url: str,
) -> None:
    with postgres_session(postgres_database_url) as session:
        task = asyncio.run(
            TaskRepository(session).create_task("Failed run", user_id="alice", status="failed")
        )
        run = asyncio.run(RunService(session).ensure_run(task.id))
        history = run.to_dict()
        run_id = run.id
        engine = session.get_bind()
        ready = Barrier(2)

        def retry(_: int) -> dict[str, Any]:
            with Session(engine) as worker:
                ready.wait(timeout=5)
                return asyncio.run(retry_run(TaskRepository(worker), run_id, "retry-once", "alice"))

        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(retry, [0, 1]))
        assert receipts[0] == receipts[1]
        session.refresh(run)
        assert run.to_dict() == history
        assert len(session.scalars(select(WorkflowRun)).all()) == 2
        assert len(session.scalars(select(TaskModel)).all()) == 2
        assert session.scalars(select(UsageEvent)).all() == []


@pytest.mark.asyncio
async def test_postgres_run_admission_fences_cached_definition_update(
    postgres_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apflow.core.execution.errors import ValidationError

    with postgres_session(postgres_database_url) as session:
        repository = TaskRepository(session)
        root = await repository.create_task(name="Original", inputs={"value": "original"})
        fence = repository._execution_fence

        async def admit_before_lock(task_id: str, *, allow_external_cancel: bool = False) -> Any:
            with Session(bind=session.get_bind(), expire_on_commit=False) as admission:
                await RunService(admission).ensure_run(root.id)
            return await fence(task_id, allow_external_cancel=allow_external_cancel)

        monkeypatch.setattr(repository, "_execution_fence", admit_before_lock)
        with pytest.raises(ValidationError, match="definition is immutable"):
            await repository.update_task(root.id, inputs={"value": "changed"})
        session.expire_all()
        persisted = await repository.get_task_by_id(root.id)
        assert persisted is not None and persisted.inputs == {"value": "original"}
        run = session.get(WorkflowRun, root.id)
        assert run is not None
        assert run.definition_snapshot["tasks"][0]["inputs"] == persisted.inputs


@pytest.mark.parametrize("mutation", ["definition", "grandchild"])
def test_postgres_run_enrollment_snapshots_concurrent_descendant_orm_writes(
    postgres_database_url: str, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    with postgres_session(postgres_database_url) as session:
        session.add_all(
            [
                TaskModel.create({"id": "root", "name": "Root"}),
                TaskModel.create(
                    {
                        "id": "child",
                        "name": "Child",
                        "parent_id": "root",
                        "schemas": {"method": "before"},
                    }
                ),
            ]
        )
        session.commit()
        engine = session.get_bind()
        tree_read = Barrier(2, timeout=5)
        writer_committed = Barrier(2, timeout=5)

        def accept() -> dict[str, Any]:
            with Session(engine, expire_on_commit=False) as admission:
                service = RunService(admission)
                read_tree = service.repository.get_all_tasks_in_tree
                first_read = True

                async def pause_after_initial_read(root: TaskModel) -> list[TaskModel]:
                    nonlocal first_read
                    tasks = await read_tree(root)
                    if first_read:
                        first_read = False
                        tree_read.wait()
                        writer_committed.wait()
                    return tasks

                monkeypatch.setattr(
                    service.repository, "get_all_tasks_in_tree", pause_after_initial_read
                )
                run = asyncio.run(service.ensure_run("root"))
                return run.definition_snapshot

        def mutate() -> None:
            tree_read.wait()
            with Session(engine) as writer:
                child = writer.get(TaskModel, "child")
                assert child is not None and child.run_id is None
                if mutation == "definition":
                    child.schemas = {"method": "after"}
                else:
                    writer.add(
                        TaskModel.create(
                            {"id": "grandchild", "name": "Grandchild", "parent_id": "child"}
                        )
                    )
                writer.commit()
            writer_committed.wait()

        with ThreadPoolExecutor(max_workers=2) as pool:
            accepted = pool.submit(accept)
            changed = pool.submit(mutate)
            changed.result(timeout=10)
            snapshot = accepted.result(timeout=10)

        session.expire_all()
        rows = session.scalars(select(TaskModel)).all()
        definitions = {row["id"]: row for row in snapshot["tasks"]}
        assert set(definitions) == {row.id for row in rows}
        assert all(row.run_id == "root" for row in rows)
        for row in rows:
            assert definitions[row.id]["schemas"] == row.schemas
        run = asyncio.run(RunService(session).start_run("root"))
        assert run.status == "running"


@pytest.mark.parametrize("descendant", [False, True])
def test_postgres_lease_recovery_and_final_receipt_share_lock_order(
    postgres_database_url: str, monkeypatch: pytest.MonkeyPatch, descendant: bool
) -> None:
    from datetime import datetime, timedelta, timezone
    from threading import Event
    from sqlalchemy.orm import sessionmaker
    from apflow.core.distributed.config import DistributedConfig
    from apflow.core.distributed.lease_manager import LeaseManager
    from apflow.core.storage.sqlalchemy.models import DistributedNode, TaskLease

    with postgres_session(postgres_database_url) as session:
        repository = TaskRepository(session)
        root = asyncio.run(repository.create_task(name="Interrupted root"))
        member = (
            asyncio.run(repository.create_task(name="Interrupted child", parent_id=root.id))
            if descendant
            else root
        )
        run = asyncio.run(RunService(session).ensure_run(root.id))
        run.status = "running"
        member.status = "in_progress"
        member_id = member.id
        now = datetime.now(timezone.utc)
        session.add(
            DistributedNode(node_id="old", executor_types=[], status="healthy", heartbeat_at=now)
        )
        session.flush()
        session.add(
            TaskLease(
                task_id=root.id,
                node_id="old",
                lease_token="expired-lease",
                expires_at=now - timedelta(seconds=10),
            )
        )
        session.commit()
        invocation = asyncio.run(AttemptRecorder(session).start(member_id, 0, 1))
        ledger = BudgetLedger(session)
        asyncio.run(ledger.configure_scope("scope", 100))
        asyncio.run(ledger.reserve("scope", member_id, invocation, 10))
        factory = sessionmaker(session.get_bind())
        recovery_locked, release_recovery, invocation_locked = Event(), Event(), Event()
        reconcile = LeaseManager._reconcile_run
        lock_invocation = UsageRecorder._lock_invocation

        def pause_recovery(self: Any, *args: Any) -> bool:
            recovery_locked.set()
            assert release_recovery.wait(timeout=5)
            return reconcile(self, *args)

        async def observe_invocation_lock(self: Any, invocation_id: str) -> ExecutionAttempt:
            result = await lock_invocation(self, invocation_id)
            invocation_locked.set()
            return result

        monkeypatch.setattr(LeaseManager, "_reconcile_run", pause_recovery)
        monkeypatch.setattr(UsageRecorder, "_lock_invocation", observe_invocation_lock)

        def finish() -> None:
            with factory() as worker:
                asyncio.run(
                    AttemptRecorder(worker).finish(
                        invocation, "completed", token_usage={"total": 5}
                    )
                )

        manager = LeaseManager(factory, DistributedConfig())
        with ThreadPoolExecutor(max_workers=2) as pool:
            recovering = pool.submit(manager.cleanup_expired_leases)
            assert recovery_locked.wait(timeout=5)
            finishing = pool.submit(finish)
            try:
                # Before the fix this succeeds while recovery holds the task,
                # exposing the invocation -> task / task -> invocation cycle.
                invocation_locked.wait(timeout=1)
            finally:
                release_recovery.set()
            recovering.result(timeout=10)
            finishing.result(timeout=10)
        session.expire_all()
        assert sum(row.token_usage["total"] for row in session.scalars(select(UsageEvent))) == 5
        restored = session.get(TaskModel, member_id)
        assert restored is not None and restored.token_usage["total"] == 5
        balance = asyncio.run(ledger.get_scope("scope"))
        assert balance.spent_tokens == 5
        assert balance.reserved_tokens == balance.unknown_tokens == 0
