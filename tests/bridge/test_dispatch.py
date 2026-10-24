"""Durable acceptance is independent of a subscription or request lifetime."""

from typing import Any
from pathlib import Path
from sqlalchemy.orm import Session

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from apflow.core.storage.sqlalchemy.models import Base, TaskDispatch, TaskModel


@pytest.mark.asyncio
async def test_retry_and_late_receipt_keep_original_run_accounting_separate(
    sync_db_session: Session,
) -> None:
    from apflow.bridge.execution_modules import RunRetryModule
    from apflow.core.execution.dispatch import TaskDispatcher
    from apflow.core.execution.runs import RunService
    from apflow.core.storage.sqlalchemy.models import WorkflowRun
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
    from apflow.durability.attempts import AttemptRecorder
    from apflow.governance.ledger import BudgetLedger
    from apflow.governance.usage import UsageRecorder
    from sqlalchemy_session_proxy import SqlalchemySessionProxy

    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="B", user_id="alice", budget_scope_id="scope")
    first = await repository.create_task(name="A", parent_id=root.id, user_id="alice")
    await repository.update_task(root.id, dependencies=[first.id])
    runs = RunService(sync_db_session)
    await runs.start_run(root.id)
    recorder = AttemptRecorder(sync_db_session)
    ledger = BudgetLedger(sync_db_session)
    await ledger.configure_scope("scope", 100)
    assert await repository.try_claim_for_execution(first.id) is not None
    first_invocation = await recorder.start(first.id, 1, 1)
    await ledger.reserve("scope", first.id, first_invocation, 10)
    await recorder.finish(first_invocation, "completed", token_usage={"total": 3})
    await repository.transition_task(
        first.id, expected_statuses=["in_progress"], status="completed", result="reusable"
    )
    assert await repository.try_claim_for_execution(root.id) is not None
    old_invocation = await recorder.start(root.id, 1, 1)
    await ledger.reserve("scope", root.id, old_invocation, 10)
    sync_db_session.add(
        TaskDispatch(
            task_id=root.id,
            state="running",
            owner_token="expired",
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    sync_db_session.commit()
    await TaskDispatcher(lambda: sync_db_session)._recover(SqlalchemySessionProxy(sync_db_session))
    old_run = sync_db_session.get(WorkflowRun, root.id)
    assert old_run is not None and old_run.status == "reconciliation_required"
    old_history = (old_run.definition_hash, old_run.status, old_run.completed_at)
    receipt = await RunRetryModule(repository).execute(
        {
            "run_id": root.id,
            "idempotency_key": "retry-after-interruption",
            "replay_safe": True,
            "evidence_ref": "provider:replay-authorized",
        }
    )
    new_root = receipt["root_task_id"]
    await runs.start_run(new_root)
    assert await repository.try_claim_for_execution(new_root) is not None
    new_invocation = await recorder.start(new_root, 1, 1)
    await ledger.reserve("scope", new_root, new_invocation, 10)
    await recorder.finish(new_invocation, "completed", token_usage={"total": 5})
    await repository.transition_task(
        new_root, expected_statuses=["in_progress"], status="completed", result="new result"
    )
    assert await runs.finish_from_tasks(new_root) == "completed"
    await recorder.finish(old_invocation, "completed", token_usage={"total": 8})
    await recorder.finish(old_invocation, "completed", token_usage={"total": 8})
    sync_db_session.refresh(old_run)
    assert (old_run.definition_hash, old_run.status, old_run.completed_at) == old_history
    original = await repository.get_task_by_id(root.id)
    retried = await repository.get_task_by_id(new_root)
    reused = await repository.get_task_by_id(receipt["task_mapping"][first.id])
    assert original is not None and retried is not None and reused is not None
    assert original.status == "in_progress" and original.result is None
    assert original.token_usage == {"input": 0, "output": 0, "total": 8}
    assert retried.result == "new result" and retried.token_usage is not None
    assert retried.token_usage["total"] == 5
    assert reused.result == "reusable" and reused.token_usage is None
    assert (await ledger.get_scope("scope")).spent_tokens == 16
    usage = UsageRecorder(sync_db_session)
    events = await usage.list_events("alice")
    assert (
        sum(event["token_usage"]["total"] for event in events if event["run_id"] == root.id) == 11
    )
    assert (
        sum(event["token_usage"]["total"] for event in events if event["run_id"] == new_root) == 5
    )
    for event in events:
        await usage.acknowledge(event["id"], "alice")
    cursor = events[-1]["index"]
    await recorder.finish(old_invocation, "completed", token_usage={"total": 8})
    assert await usage.list_events("alice", cursor) == []


@pytest.mark.asyncio
async def test_dispatch_executes_only_accepted_root_and_complete_tree(tmp_path: Path) -> None:
    from apflow.core.execution.dispatch import TaskDispatcher, accept_task

    engine = create_engine(f"sqlite:///{tmp_path / 'dispatch.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                TaskModel.create({"id": "root", "name": "root"}),
                TaskModel.create({"id": "child", "name": "child", "parent_id": "root"}),
                TaskModel.create({"id": "draft", "name": "draft"}),
            ]
        )
        session.commit()
        assert (await accept_task(session, "root", None))["state"] == "accepted"
        assert (await accept_task(session, "root", None))["task_id"] == "root"
    calls = []

    async def execute(task_id: str, session: Session, context: Any) -> dict[str, Any]:
        calls.append(task_id)
        task = session.get(TaskModel, task_id)
        assert task is not None
        task.status = "completed"
        child = session.get(TaskModel, "child")
        assert child is not None
        child.status = "completed"
        session.commit()
        return {"status": "completed"}

    dispatcher = TaskDispatcher(factory, execute=execute)
    await dispatcher.run_once()
    assert calls == ["root"]
    with factory() as session:
        draft = session.get(TaskModel, "draft")
        dispatch = session.get(TaskDispatch, "root")
        assert draft is not None and draft.status == "pending"
        assert dispatch is not None and dispatch.state == "finished"
    engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_recovery_preserves_unknown_remote_outcome(tmp_path: Path) -> None:
    from apflow.core.execution.dispatch import TaskDispatcher

    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(TaskModel.create({"id": "root", "name": "root", "status": "in_progress"}))
        session.add(
            TaskDispatch(
                task_id="root",
                state="running",
                owner_token="expired",
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
            )
        )
        session.commit()
    dispatcher = TaskDispatcher(factory)
    await dispatcher.run_once()
    with factory() as session:
        dispatch = session.scalar(select(TaskDispatch))
        assert dispatch is not None
        assert dispatch.state == "reconciliation_required"
        assert dispatch.owner_token is None
    engine.dispose()


@pytest.mark.asyncio
async def test_real_dispatcher_executes_persisted_tree_after_reconstruction(tmp_path: Path) -> None:
    from apflow.app import create_app
    from apflow.core.base import BaseTask
    from apflow.core.execution.dispatch import TaskDispatcher, accept_task
    from apflow.core.execution.task_creator import TaskCreator
    from apflow.core.extensions import get_registry

    calls: list[str] = []

    class RecordedExecutor(BaseTask):
        id = "dispatch_recorded_executor"
        name = "Record dispatch"
        description = "Synthetic execution without external side effects"
        result_format = "envelope"

        async def execute(self, inputs: dict) -> dict:
            calls.append(inputs["step"])
            return {"result": inputs["step"]}

    extensions = get_registry()
    extensions.register(RecordedExecutor(), executor_class=RecordedExecutor, override=True)
    app = create_app(connection_string=f"sqlite:///{tmp_path / 'actual.db'}")
    try:
        tree = await TaskCreator(app.session).create_task_tree_from_array(
            [
                {
                    "name": "root",
                    "params": {"executor_id": RecordedExecutor.id},
                    "inputs": {"step": "root"},
                },
                {
                    "name": "first",
                    "parent_id": "root",
                    "params": {"executor_id": RecordedExecutor.id},
                    "inputs": {"step": "first"},
                },
                {
                    "name": "second",
                    "parent_id": "root",
                    "dependencies": [{"name": "first"}],
                    "params": {"executor_id": RecordedExecutor.id},
                    "inputs": {"step": "second"},
                },
            ]
        )
        root_id = tree.task.id
        await accept_task(app.session, root_id, None)
        dispatcher = TaskDispatcher(app.session_factory)
        await dispatcher.run_once()
        assert "first" in calls and "second" in calls
        assert calls.index("first") < calls.index("second")
        before = list(calls)
        await TaskDispatcher(app.session_factory).run_once()
        assert calls == before
        with app.session_factory() as session:
            dispatch = session.get(TaskDispatch, root_id)
            assert dispatch is not None and dispatch.state == "finished"
    finally:
        extensions.unregister(RecordedExecutor.id)
        app.session.close()


@pytest.mark.asyncio
async def test_recovery_does_not_replay_pending_parent_with_started_child(tmp_path: Path) -> None:
    from apflow.core.execution.dispatch import TaskDispatcher

    engine = create_engine(f"sqlite:///{tmp_path / 'interrupted-child.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                TaskModel.create({"id": "root", "name": "root"}),
                TaskModel.create(
                    {"id": "child", "name": "child", "parent_id": "root", "status": "in_progress"}
                ),
            ]
        )
        session.add(
            TaskDispatch(
                task_id="root",
                state="running",
                owner_token="old",
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        session.commit()
    await TaskDispatcher(factory).run_once()
    with factory() as session:
        dispatch = session.get(TaskDispatch, "root")
        assert dispatch is not None and dispatch.state == "reconciliation_required"
    engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_and_legacy_worker_cannot_both_own_root(tmp_path: Path) -> None:
    from apcore.errors import ModuleError
    from apflow.core.distributed.config import DistributedConfig
    from apflow.core.distributed.lease_manager import LeaseManager
    from apflow.core.execution.dispatch import accept_task

    engine = create_engine(f"sqlite:///{tmp_path / 'owners.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                TaskModel.create({"id": "worker-root", "name": "root"}),
                TaskModel.create({"id": "dispatch-root", "name": "root"}),
            ]
        )
        session.commit()
    leases = LeaseManager(factory, DistributedConfig())
    assert leases.acquire_lease("worker-root", "worker") is not None
    with factory() as session:
        with pytest.raises(ModuleError, match="distributed worker"):
            await accept_task(session, "worker-root", None)
        session.rollback()
        await accept_task(session, "dispatch-root", None)
    assert leases.acquire_lease("dispatch-root", "worker") is None
    engine.dispose()


@pytest.mark.asyncio
async def test_expired_dispatch_closes_running_attempt_as_unknown(tmp_path: Path) -> None:
    from apflow.core.execution.dispatch import TaskDispatcher
    from apflow.core.storage.sqlalchemy.models import ExecutionAttempt

    engine = create_engine(f"sqlite:///{tmp_path / 'attempt-recovery.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(TaskModel.create({"id": "root", "name": "root", "status": "in_progress"}))
        session.add(
            TaskDispatch(
                task_id="root",
                state="running",
                owner_token="old",
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        session.add(
            ExecutionAttempt(
                id="attempt",
                task_id="root",
                attempt_id=0,
                attempt_number=0,
                status="running",
                started_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    await TaskDispatcher(factory).run_once()
    with factory() as session:
        attempt = session.get(ExecutionAttempt, "attempt")
        assert attempt is not None and attempt.status == "unknown"
        assert attempt.completed_at is not None
    engine.dispose()


@pytest.mark.asyncio
async def test_submit_rejects_tree_with_running_child(sync_db_session: Session) -> None:
    from apcore.errors import ModuleError
    from apflow.core.execution.dispatch import accept_task
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="root")
    await repository.create_task(name="child", parent_id=root.id, status="in_progress")
    with pytest.raises(ModuleError, match="active"):
        await accept_task(sync_db_session, root.id, None)


@pytest.mark.asyncio
async def test_scheduler_cannot_clone_an_accepted_execution(sync_db_session: Session) -> None:
    from apflow.core.execution.dispatch import accept_task
    from apflow.core.execution.task_creator import TaskCreator
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="root")
    await accept_task(sync_db_session, root.id, None)
    with pytest.raises(ValueError, match="submitted"):
        await TaskCreator(sync_db_session).instantiate_scheduled_run(root)


def test_worker_does_not_lease_deleted_task(tmp_path: Path) -> None:
    from apflow.core.distributed.config import DistributedConfig
    from apflow.core.distributed.lease_manager import LeaseManager

    engine = create_engine(f"sqlite:///{tmp_path / 'deleted-root.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        assert LeaseManager(factory, DistributedConfig()).acquire_lease("deleted", "worker") is None
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_capacity_leaves_excess_work_durably_queued(tmp_path: Path) -> None:
    import asyncio
    from apflow.core.execution.dispatch import TaskDispatcher, accept_task

    engine = create_engine(f"sqlite:///{tmp_path / 'capacity.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        for identifier in ("one", "two", "three"):
            session.add(TaskModel.create({"id": identifier, "name": identifier}))
        session.commit()
        for identifier in ("one", "two", "three"):
            await accept_task(session, identifier, None)

    async def execute(task_id: str, session: Session, context: Any) -> None:
        await asyncio.Event().wait()

    dispatcher = TaskDispatcher(factory, execute=execute, max_concurrent=2)
    try:
        await dispatcher._dispatch()
        await dispatcher._dispatch()
        assert len(dispatcher._running) == 2
        with factory() as session:
            states = [row.state for row in session.scalars(select(TaskDispatch))]
            assert states.count("accepted") == 1
            assert states.count("running") == 2
    finally:
        await dispatcher.stop()
        engine.dispose()
