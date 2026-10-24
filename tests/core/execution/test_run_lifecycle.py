"""A durable run keeps an immutable definition and cannot be executed twice."""

import pytest
from datetime import datetime, timedelta, timezone
from apcore.errors import ModuleError
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session, sessionmaker

from apflow.core.storage.sqlalchemy.models import Base, TaskDispatch, TaskModel, WorkflowRun


@pytest.mark.asyncio
async def test_run_snapshots_definition_and_binds_every_node() -> None:
    from apflow.core.execution.runs import RunService

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        root = TaskModel.create({"id": "root", "name": "root"})
        child = TaskModel.create({"id": "child", "name": "child", "parent_id": "root"})
        session.add_all([root, child])
        session.commit()
        run = await RunService(session).ensure_run("root")
        original = run.definition_hash
        assert root.run_id == child.run_id == run.id
        assert len(run.definition_snapshot["tasks"]) == 2
        root.name = "changed"
        from apflow.core.execution.errors import ValidationError

        with pytest.raises(ValidationError, match="immutable"):
            session.commit()
        session.rollback()
        assert (await RunService(session).ensure_run("root")).definition_hash == original
    engine.dispose()


@pytest.mark.asyncio
async def test_run_enrollment_preserves_archived_dependency_history() -> None:
    from apflow.core.execution.runs import RunService

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        root = TaskModel.create({"id": "root", "name": "root"})
        frozen = TaskModel.create(
            {
                "id": "frozen",
                "name": "frozen",
                "parent_id": "root",
                "origin_type": "archive",
                "status": "completed",
                "result": {"value": 1},
            }
        )
        session.add_all([root, frozen])
        session.commit()
        run = await RunService(session).ensure_run("root")
        assert frozen.run_id == run.id
        assert frozen.result == {"value": 1} and frozen.status == "completed"
    engine.dispose()


@pytest.mark.asyncio
async def test_expired_owner_cannot_finalize_run() -> None:
    from apcore import Context
    from apflow.core.execution.context import execution_context
    from apflow.core.execution.runs import RunService

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(TaskModel.create({"id": "root", "name": "root"}))
        session.commit()
        await RunService(session).start_run("root")
        session.add(
            TaskDispatch(
                task_id="root",
                state="running",
                owner_token="new-owner",
                lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            )
        )
        session.commit()
        marker = execution_context.set(
            Context.create(data={"dispatch_task_id": "root", "dispatch_token": "old-owner"})
        )
        try:
            await RunService(session).finish_run("root", "completed")
        finally:
            execution_context.reset(marker)
        session.expire_all()
        run = session.get(WorkflowRun, "root")
        assert run is not None and run.status == "running"
    engine.dispose()


@pytest.mark.asyncio
async def test_dispatch_recovery_preserves_unknown_budget_liability() -> None:
    from apflow.core.execution.dispatch import TaskDispatcher
    from apflow.core.storage.sqlalchemy.models import (
        BudgetAccount,
        BudgetReservation,
        ExecutionAttempt,
    )
    from sqlalchemy_session_proxy import SqlalchemySessionProxy

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                TaskModel.create({"id": "root", "name": "root", "status": "in_progress"}),
                TaskDispatch(
                    task_id="root",
                    state="running",
                    owner_token="expired",
                    lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
                ),
                BudgetAccount(scope_id="scope", limit_tokens=10, reserved_tokens=5),
                BudgetReservation(
                    id="invocation",
                    scope_id="scope",
                    task_id="root",
                    state="reserved",
                    reserved_tokens=5,
                ),
                ExecutionAttempt(
                    id="invocation",
                    task_id="root",
                    run_id="root",
                    attempt_id=0,
                    attempt_number=1,
                    status="running",
                    started_at=datetime.now(timezone.utc),
                ),
            ]
        )
        session.commit()
        dispatcher = TaskDispatcher(factory)
        await dispatcher._recover(SqlalchemySessionProxy(session))
        await dispatcher._recover(SqlalchemySessionProxy(session))
        session.expire_all()
        account = session.get(BudgetAccount, "scope")
        reservation = session.get(BudgetReservation, "invocation")
        assert account is not None and reservation is not None
        assert account.reserved_tokens == 0 and account.unknown_tokens == 5
        assert reservation.state == "unknown" and reservation.actual_tokens is None
    engine.dispose()


@pytest.mark.asyncio
async def test_accepted_run_rejects_modified_executor_definition() -> None:
    from apflow.core.execution.runs import RunService

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        root = TaskModel.create({"id": "root", "name": "root", "schemas": {"method": "first"}})
        session.add(root)
        session.commit()
        await RunService(session).ensure_run("root")
        # Simulate an unsupported bulk writer bypassing normal ORM guards.
        session.execute(
            update(TaskModel).where(TaskModel.id == root.id).values(schemas={"method": "second"})
        )
        session.commit()
        with pytest.raises(ModuleError, match="definition changed"):
            await RunService(session).start_run("root")
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [False, True])
async def test_dispatch_requeues_only_when_no_invocation_started(started: bool) -> None:
    from apflow.core.execution.dispatch import TaskDispatcher
    from apflow.core.execution.runs import RunService
    from sqlalchemy_session_proxy import SqlalchemySessionProxy

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        root = TaskModel.create({"id": "root", "name": "root"})
        session.add(root)
        if started:
            session.add(TaskModel.create({"id": "child", "name": "child", "parent_id": "root"}))
        session.commit()
        await RunService(session).start_run("root")
        if started:
            root.status = "completed"
            root.started_at = datetime.now(timezone.utc)
        session.add(
            TaskDispatch(
                task_id="root",
                state="running",
                owner_token="expired",
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
            )
        )
        session.commit()
        await TaskDispatcher(factory)._recover(SqlalchemySessionProxy(session))
        session.expire_all()
        run = session.get(WorkflowRun, "root")
        dispatch = session.get(TaskDispatch, "root")
        assert run is not None and dispatch is not None
        expected = "reconciliation_required" if started else "accepted"
        assert run.status == expected
        assert dispatch.state == expected
    engine.dispose()


@pytest.mark.asyncio
async def test_terminal_run_rejects_another_start_without_resetting_tasks() -> None:
    from apflow.core.execution.runs import RunService

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        root = TaskModel.create({"id": "root", "name": "root", "status": "failed"})
        session.add(root)
        session.commit()
        service = RunService(session)
        run = await service.ensure_run("root")
        assert run.status == "failed"
        with pytest.raises(ModuleError, match="new run"):
            await service.start_run("root")
        assert root.status == "failed"
    engine.dispose()


@pytest.mark.asyncio
async def test_retry_dispatch_executes_children_below_frozen_completed_root(
    sync_db_session: Session,
) -> None:
    from apcore import Context, Identity
    from apflow.bridge.execution_modules import RunRetryModule
    from apflow.core.base import BaseTask
    from apflow.core.execution.dispatch import TaskDispatcher, accept_task
    from apflow.core.execution.runs import RunService
    from apflow.core.extensions import get_registry
    from apflow.core.storage.session import session_factory_for
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    calls: list[str] = []

    class RetryProbe(BaseTask):
        id = "run_retry_lifecycle_probe"
        name = "Retry lifecycle probe"
        description = "Record actual child invocations below a frozen root"
        result_format = "envelope"

        async def execute(self, inputs: dict) -> dict:
            calls.append(inputs["step"])
            return {"result": inputs["step"]}

    registry = get_registry()
    registry.register(RetryProbe(), executor_class=RetryProbe, override=True)
    repository = TaskRepository(sync_db_session)
    try:
        root = await repository.create_task(
            name="root", user_id="alice", status="completed", result={"ok": True}
        )
        child = await repository.create_task(
            name="child",
            user_id="alice",
            parent_id=root.id,
            task_tree_id=root.id,
            status="failed",
            params={"executor_id": RetryProbe.id},
            inputs={"step": "child"},
        )
        await RunService(sync_db_session).ensure_run(root.id)
        context = Context.create(identity=Identity(id="alice"))
        receipt = await RunRetryModule(repository).execute(
            {"run_id": root.id, "idempotency_key": "new-run"}, context
        )
        await accept_task(sync_db_session, receipt["run_id"], context)
        await TaskDispatcher(session_factory_for(sync_db_session)).run_once()
        sync_db_session.expire_all()
        new_run = sync_db_session.get(WorkflowRun, receipt["run_id"])
        assert new_run is not None and new_run.status == "completed"
        assert calls == ["child"]
        historical_child = await repository.get_task_by_id(child.id)
        assert historical_child is not None and historical_child.status == "failed"
        await TaskDispatcher(session_factory_for(sync_db_session)).run_once()
        assert calls == ["child"]
    finally:
        registry.unregister(RetryProbe.id)
