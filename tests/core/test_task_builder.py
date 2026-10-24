import pytest

from apflow.core.base import BaseTask
from apflow.core.builders import TaskBuilder
from apflow.core.extensions.decorators import executor_register
from apflow.core.extensions.registry import get_registry
from apflow.core.execution.task_manager import TaskManager
from apflow.core.execution.task_executor import TaskExecutor
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


@executor_register()
class _DummyExecutor(BaseTask):
    id = "dummy_executor"
    name = "Dummy Executor"
    description = "Returns inputs for testing"

    @property
    def type(self) -> str:
        return "test"

    def get_input_schema(self):
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        }

    def get_output_schema(self):
        return {
            "type": "object",
            "properties": {"echo": {"type": "object"}},
            "required": ["echo"],
        }

    async def execute(self, inputs):
        return {"echo": inputs}


@pytest.fixture(autouse=True)
def _reset_dummy_executor():
    registry = get_registry()
    registry.register(_DummyExecutor(), executor_class=_DummyExecutor, override=True)
    yield
    registry.unregister("dummy_executor")


@pytest.mark.asyncio
async def test_task_builder_executes_with_registered_executor(sync_db_session):
    task_manager = TaskManager(sync_db_session)
    builder = (
        TaskBuilder(task_manager=task_manager, executor_id="dummy_executor")
        .with_name("dummy task")
        .with_input("value", "hello")
        .enable_streaming()
    )

    result = await builder.execute()

    assert result["status"] == "completed"
    repo = TaskRepository(sync_db_session)
    stored = await repo.get_task_by_id(result["root_task_id"])
    assert stored is not None
    assert stored.result == {"echo": {"value": "hello"}}


@pytest.mark.asyncio
async def test_task_builder_executes_with_multiple_dependencies(sync_db_session):
    task_manager = TaskManager(sync_db_session)
    repo = TaskRepository(sync_db_session)

    root = await repo.create_task(
        name="root",
        user_id="user",
        inputs={},
        schemas={"method": "dummy_executor"},
    )

    dep1 = await repo.create_task(
        name="dep1",
        user_id="user",
        inputs={},
        schemas={"method": "dummy_executor"},
        parent_id=root.id,
    )
    dep1.status = "completed"
    dep1.result = {"url": "https://one", "data": "d1"}

    dep2 = await repo.create_task(
        name="dep2",
        user_id="user",
        inputs={},
        schemas={"method": "dummy_executor"},
        parent_id=root.id,
    )
    dep2.status = "completed"
    dep2.result = {"token": "t2"}
    sync_db_session.commit()

    builder = (
        TaskBuilder(task_manager=task_manager, executor_id="dummy_executor")
        .with_name("root-dependent")
        .with_parent(root.id)
        .with_inputs({"base": "ok"})
        .depends_on(str(dep1.id), str(dep2.id))
    )

    built = await builder.build()
    result = await TaskExecutor().execute_task_by_id(root.id, db_session=sync_db_session)

    assert result["status"] == "completed"
    stored = await repo.get_task_by_id(built.task.id)
    assert stored is not None
    assert stored.result["echo"]["base"] == "ok"
    assert {d["id"] for d in stored.dependencies} == {str(dep1.id), str(dep2.id)}


@pytest.mark.asyncio
async def test_task_builder_executes_with_multi_level_dependencies(sync_db_session):
    task_manager = TaskManager(sync_db_session)
    repo = TaskRepository(sync_db_session)

    root = await repo.create_task(
        name="root",
        user_id="user",
        inputs={},
        schemas={"method": "dummy_executor"},
    )

    dep_a = await repo.create_task(
        name="dep-a",
        user_id="user",
        inputs={},
        schemas={"method": "dummy_executor"},
        parent_id=root.id,
    )
    dep_a.status = "completed"
    dep_a.result = {"a": 1}

    dep_b = await repo.create_task(
        name="dep-b",
        user_id="user",
        inputs={},
        schemas={"method": "dummy_executor"},
        parent_id=root.id,
        dependencies=[{"id": str(dep_a.id), "required": True}],
    )
    dep_b.status = "completed"
    dep_b.result = {"b": 2, "a": 1}
    sync_db_session.commit()

    builder = (
        TaskBuilder(task_manager=task_manager, executor_id="dummy_executor")
        .with_name("chain-c")
        .with_parent(root.id)
        .with_inputs({"c": 3})
        .depends_on(str(dep_b.id))
    )

    built = await builder.build()
    result = await TaskExecutor().execute_task_by_id(root.id, db_session=sync_db_session)

    assert result["status"] == "completed"
    stored = await repo.get_task_by_id(built.task.id)
    assert stored is not None
    assert stored.result["echo"]["c"] == 3
    assert stored.dependencies == [{"id": str(dep_b.id), "required": True}]


@pytest.mark.asyncio
async def test_task_builder_requires_executor_id(sync_db_session):
    with pytest.raises(ValueError):
        TaskBuilder(task_manager=TaskManager(sync_db_session), executor_id="missing").with_name(
            "test"
        )


@pytest.mark.asyncio
async def test_task_builder_requires_name(sync_db_session):
    builder = TaskBuilder(task_manager=TaskManager(sync_db_session), executor_id="dummy_executor")
    with pytest.raises(ValueError):
        await builder.build()
