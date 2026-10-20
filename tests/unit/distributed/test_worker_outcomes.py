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
