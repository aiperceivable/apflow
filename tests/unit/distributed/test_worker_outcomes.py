"""Real-storage regressions for worker selection and execution outcomes."""

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from apflow.core.distributed.config import DistributedConfig
from apflow.core.distributed.idempotency import IdempotencyManager
from apflow.core.distributed.lease_manager import LeaseManager
from apflow.core.distributed.node_registry import NodeRegistry
from apflow.core.distributed.types import TaskExecutorFn
from apflow.core.distributed.worker import WorkerRuntime
from apflow.core.storage.sqlalchemy.models import Base, ExecutionIdempotency, TaskModel


@pytest.fixture
def worker_sessions() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _worker(sessions: sessionmaker[Session], execute: TaskExecutorFn) -> WorkerRuntime:
    config = DistributedConfig(node_id="local", max_parallel_tasks_per_node=2)
    nodes = NodeRegistry(sessions, config)
    nodes.register_node("local", ["default"])
    return WorkerRuntime(
        "local",
        config,
        sessions,
        nodes,
        LeaseManager(sessions, config),
        IdempotencyManager(sessions),
        execute,
    )


def test_selection_scans_past_ineligible_pending_prefix(
    worker_sessions: sessionmaker[Session],
) -> None:
    async def execute(task: TaskModel) -> dict[str, Any]:
        return {"status": "completed"}

    worker = _worker(worker_sessions, execute)
    with worker_sessions() as session:
        session.add_all(
            [
                TaskModel(
                    id="a", name="Other node", placement_constraints={"allowed_nodes": ["other"]}
                ),
                TaskModel(
                    id="b", name="Other node", placement_constraints={"allowed_nodes": ["other"]}
                ),
                TaskModel(id="c", name="Local node"),
            ]
        )
        session.commit()
    assert [task.id for task in worker._find_executable_tasks()] == ["c"]


@pytest.mark.asyncio
async def test_failed_outcome_is_not_completed_or_cached_as_success(
    worker_sessions: sessionmaker[Session],
) -> None:
    async def execute(task: TaskModel) -> dict[str, Any]:
        with worker_sessions() as session:
            stored = session.get(TaskModel, task.id)
            assert stored is not None
            stored.status = "failed"
            stored.error = "Executor failed"
            session.commit()
        return {"status": "failed", "error": "Executor failed"}

    worker = _worker(worker_sessions, execute)
    with worker_sessions() as session:
        task = TaskModel(id="failed-task", name="Failed task")
        session.add(task)
        session.commit()
        session.expunge(task)
    await worker._execute_task(task)
    with worker_sessions() as session:
        stored = session.get(TaskModel, task.id)
        assert stored is not None and stored.status == "failed"
        cached = session.query(ExecutionIdempotency).filter_by(task_id=task.id).one()
        assert cached.status == "failed"


def test_worker_selects_only_unowned_runnable_roots(
    worker_sessions: sessionmaker[Session],
) -> None:
    from apflow.core.storage.sqlalchemy.models import TaskDispatch

    async def execute(task: TaskModel) -> dict[str, Any]:
        return {"status": "completed"}

    worker = _worker(worker_sessions, execute)
    with worker_sessions() as session:
        session.add_all(
            [
                TaskModel(id="root", name="Root"),
                TaskModel(id="child", name="Child", parent_id="root"),
                TaskModel(id="detached", name="Detached"),
                TaskModel(id="schedule-run", name="Run", origin_type="scheduled_run"),
            ]
        )
        session.add(TaskDispatch(task_id="detached", state="accepted"))
        session.commit()
    assert {task.id for task in worker._find_executable_tasks()} == {"root", "schedule-run"}


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_ownership", ["reassigned", "expired", "dispatch_owned"])
async def test_worker_fence_rejects_inner_write_after_lease_reassignment(
    worker_sessions: sessionmaker[Session],
    lost_ownership: str,
) -> None:
    from apflow.core.execution.context import execution_context
    from datetime import datetime, timedelta, timezone
    from apflow.core.storage.sqlalchemy.models import TaskLease, TaskDispatch
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    observed: list[bool] = []

    async def execute(task: TaskModel) -> dict[str, Any]:
        context = execution_context.get()
        assert context.data["worker_root_id"] == task.id
        with worker_sessions() as session:
            lease = session.get(TaskLease, task.id)
            assert lease is not None
            if lost_ownership == "reassigned":
                lease.lease_token = "replacement-worker-token"
            elif lost_ownership == "expired":
                lease.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            else:
                session.add(TaskDispatch(task_id=task.id, state="accepted"))
            session.commit()
        with worker_sessions() as session:
            result = await TaskRepository(session).transition_task(
                task.id, expected_statuses=["pending"], status="completed", result="stale"
            )
            observed.append(result is None)
        return {"status": "completed", "result": "stale"}

    worker = _worker(worker_sessions, execute)
    with worker_sessions() as session:
        task = TaskModel(id="stale-root", name="Stale root")
        session.add(task)
        session.commit()
        session.expunge(task)
    await worker._execute_task(task)
    assert observed == [True]
    with worker_sessions() as session:
        stored = session.get(TaskModel, task.id)
        assert stored is not None and stored.status == "pending"
        assert stored.result is None
        assert session.query(ExecutionIdempotency).filter_by(task_id=task.id).count() == 0


@pytest.mark.asyncio
async def test_worker_does_not_complete_cancelled_outcome(
    worker_sessions: sessionmaker[Session],
) -> None:
    from datetime import datetime, timezone

    async def execute(task: TaskModel) -> dict[str, Any]:
        with worker_sessions() as session:
            stored = session.get(TaskModel, task.id)
            assert stored is not None
            stored.cancel_requested_at = datetime.now(timezone.utc)
            stored.status = "cancelled"
            session.commit()
        return {"status": "cancelled", "result": None}

    worker = _worker(worker_sessions, execute)
    with worker_sessions() as session:
        task = TaskModel(id="cancelled-root", name="Cancelled root")
        session.add(task)
        session.commit()
        session.expunge(task)
    await worker._execute_task(task)
    with worker_sessions() as session:
        stored = session.get(TaskModel, task.id)
        assert stored is not None and stored.status == "cancelled"
        assert (
            session.query(ExecutionIdempotency)
            .filter_by(task_id=task.id, status="completed")
            .count()
            == 0
        )


@pytest.mark.asyncio
async def test_worker_uses_persisted_identity_not_task_inputs(
    worker_sessions: sessionmaker[Session],
) -> None:
    from apflow.core.execution.context import execution_context

    seen: list[tuple[str, list[str]]] = []

    async def execute(task: TaskModel) -> dict[str, Any]:
        identity = execution_context.get().identity
        seen.append((identity.id, list(identity.roles)))
        return {"status": "completed"}

    worker = _worker(worker_sessions, execute)
    with worker_sessions() as session:
        task = TaskModel(
            id="identity-root",
            name="Identity root",
            user_id="alice",
            inputs={"roles": ["apflow.admin"]},
            execution_identity={"id": "alice", "type": "user", "roles": ["operator"], "attrs": {}},
        )
        session.add(task)
        session.commit()
        session.expunge(task)
    await worker._execute_task(task)
    assert seen == [("alice", ["operator"])]
