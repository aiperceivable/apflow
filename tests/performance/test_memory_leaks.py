"""Exercise executor cleanup, process memory, and SQLite reclamation through current APIs."""

import asyncio
from collections.abc import Callable, Iterator
import gc
import logging
import os
from pathlib import Path
from typing import Any, ClassVar
from weakref import WeakSet

import pytest
from sqlalchemy import Engine, delete, func, select, text
from sqlalchemy.orm import Session

from apflow.core.base import BaseTask
from apflow.core.execution.task_manager import TaskManager
from apflow.core.extensions import get_registry
from apflow.core.storage.factory import create_session
from apflow.core.storage.sqlalchemy.models import TaskModel
from apflow.core.types import TaskTreeNode

psutil = pytest.importorskip("psutil", reason="psutil is required for memory measurements")
logger = logging.getLogger(__name__)
pytestmark = [pytest.mark.slow, pytest.mark.timeout(120)]


class MemoryProbeExecutor(BaseTask):
    """Keep weak instance references so the test cannot itself retain completed executors."""

    id = "memory_probe_executor"
    name = "Memory probe"
    description = "Yield once and return a small result or an explicit test failure."
    cancelable = True
    instances: ClassVar[WeakSet["MemoryProbeExecutor"]] = WeakSet()
    executions: ClassVar[int] = 0

    def __init__(self, inputs: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(inputs=inputs, **kwargs)
        self.instances.add(self)

    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        type(self).executions += 1
        await asyncio.sleep(0)
        if inputs.get("fail"):
            raise RuntimeError("Expected memory-test execution failure")
        return {"value": inputs.get("value")}


@pytest.fixture
def memory_executor() -> Iterator[type[MemoryProbeExecutor]]:
    MemoryProbeExecutor.instances = WeakSet()
    MemoryProbeExecutor.executions = 0
    registry = get_registry()
    registry.register(MemoryProbeExecutor(), executor_class=MemoryProbeExecutor)
    try:
        yield MemoryProbeExecutor
    finally:
        registry.unregister(MemoryProbeExecutor.id)


@pytest.fixture
def workflow_session() -> Iterator[Session]:
    session = create_session(path=":memory:")
    assert isinstance(session, Session)
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def get_memory_mb() -> Callable[[], float]:
    process = psutil.Process(os.getpid())

    def measure_memory() -> float:
        gc.collect()
        return process.memory_info().rss / 1024 / 1024

    return measure_memory


async def create_probe_task(manager: TaskManager, value: int, *, fail: bool = False) -> TaskModel:
    return await manager.task_repository.create_task(
        "Memory test task",
        params={"executor_id": MemoryProbeExecutor.id},
        inputs={"value": value, "fail": fail},
    )


class TestMemoryLeaks:
    @pytest.mark.asyncio
    async def test_executor_instances_cleanup(
        self, workflow_session: Session, memory_executor: type[MemoryProbeExecutor]
    ) -> None:
        manager = TaskManager(workflow_session, pre_hooks=[], post_hooks=[])
        for value in range(100):
            task = await create_probe_task(manager, value)
            tree = await manager.distribute_task_tree(TaskTreeNode(task), use_callback=False)
            assert tree.task.status == "completed"
            assert manager._executor_instances == {}
        gc.collect()
        assert memory_executor.executions == 100
        assert len(memory_executor.instances) == 1
        assert all(instance.task is None for instance in memory_executor.instances)

    @pytest.mark.asyncio
    async def test_executor_instances_cleanup_on_failure(
        self, workflow_session: Session, memory_executor: type[MemoryProbeExecutor]
    ) -> None:
        manager = TaskManager(workflow_session, pre_hooks=[], post_hooks=[])
        task = await create_probe_task(manager, 0, fail=True)

        tree = await manager.distribute_task_tree(TaskTreeNode(task), use_callback=False)

        assert tree.task.status == "failed"
        assert tree.task.error == "Expected memory-test execution failure"
        assert memory_executor.executions == 1
        assert manager._executor_instances == {}
        assert all(instance.task is None for instance in memory_executor.instances)

    @pytest.mark.asyncio
    async def test_memory_stable_over_1000_tasks(
        self,
        workflow_session: Session,
        memory_executor: type[MemoryProbeExecutor],
        get_memory_mb: Callable[[], float],
    ) -> None:
        manager = TaskManager(workflow_session, pre_hooks=[], post_hooks=[])
        initial_memory = get_memory_mb()
        for value in range(1000):
            task = await create_probe_task(manager, value)
            tree = await manager.distribute_task_tree(TaskTreeNode(task), use_callback=False)
            assert tree.task.status == "completed"
            if (value + 1) % 100 == 0:
                growth = get_memory_mb() - initial_memory
                logger.info("Completed %s tasks; memory growth %.1f MB", value + 1, growth)
                assert growth < 100
        assert memory_executor.executions == 1000
        assert manager._executor_instances == {}
        assert get_memory_mb() - initial_memory < 100
        assert workflow_session.scalar(select(func.count()).select_from(TaskModel)) == 1000

    @pytest.mark.asyncio
    async def test_concurrent_tasks_memory(
        self,
        workflow_session: Session,
        memory_executor: type[MemoryProbeExecutor],
        get_memory_mb: Callable[[], float],
    ) -> None:
        manager = TaskManager(workflow_session, pre_hooks=[], post_hooks=[])
        initial_memory = get_memory_mb()
        for batch in range(100):
            tasks = [await create_probe_task(manager, batch * 10 + item) for item in range(10)]
            trees = await asyncio.gather(
                *(
                    manager.distribute_task_tree(TaskTreeNode(task), use_callback=False)
                    for task in tasks
                )
            )
            assert all(tree.task.status == "completed" for tree in trees)
            assert manager._executor_instances == {}
            if (batch + 1) % 10 == 0:
                assert get_memory_mb() - initial_memory < 150
        assert memory_executor.executions == 1000
        assert get_memory_mb() - initial_memory < 150
        assert workflow_session.scalar(select(func.count()).select_from(TaskModel)) == 1000


class TestDatabaseGrowth:
    def test_sqlite_file_growth_reasonable(self, tmp_path: Path) -> None:
        database_path = tmp_path / "growth.db"
        session = create_session(path=database_path)
        assert isinstance(session, Session)
        engine = session.get_bind()
        assert isinstance(engine, Engine)
        try:
            with session:
                for index in range(1000):
                    session.add(
                        TaskModel.create(
                            {
                                "id": f"task_{index}",
                                "name": "Size test task",
                                "status": "completed",
                                "inputs": {
                                    "value": index,
                                    "description": "Test task for size testing",
                                },
                                "result": {"output": f"Result {index}"},
                            }
                        )
                    )
                    if (index + 1) % 100 == 0:
                        session.commit()
                assert session.scalar(select(func.count()).select_from(TaskModel)) == 1000
        finally:
            engine.dispose()
        assert database_path.exists()
        assert 0 < database_path.stat().st_size < 50 * 1024 * 1024

    def test_task_cleanup_reduces_size(self, tmp_path: Path) -> None:
        database_path = tmp_path / "cleanup.db"
        session = create_session(path=database_path)
        assert isinstance(session, Session)
        engine = session.get_bind()
        assert isinstance(engine, Engine)
        try:
            with session:
                session.add_all(
                    TaskModel.create(
                        {
                            "id": f"task_{index}",
                            "name": "Cleanup test task",
                            "status": "completed",
                            "result": {"output": "x" * 4096},
                        }
                    )
                    for index in range(500)
                )
                session.commit()
                session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
                session.commit()
                size_after_insert = database_path.stat().st_size

                session.execute(
                    delete(TaskModel).where(
                        TaskModel.id.in_([f"task_{index}" for index in range(250)])
                    )
                )
                session.commit()
                assert session.scalar(select(func.count()).select_from(TaskModel)) == 250
                session.commit()
                session.execute(text("VACUUM"))
                session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
                session.commit()
        finally:
            engine.dispose()
        assert 0 < database_path.stat().st_size < size_after_insert
