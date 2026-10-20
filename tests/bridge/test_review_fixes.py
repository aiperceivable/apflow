"""Regressions for bridge mutations and runtime executor registration."""

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apcore.errors import ModuleError
from sqlalchemy.orm import Session

from apflow.bridge.scanner_bridge import discover_executor_modules
from apflow.bridge.task_modules import TaskDeleteModule, TaskUpdateModule
from apflow.core.base import BaseTask
from apflow.core.extensions import executor_register, get_registry
from apflow.core.extensions.types import ExtensionCategory
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


@pytest.fixture
def registered_ids() -> Iterator[list[str]]:
    ids: list[str] = []
    yield ids
    for executor_id in ids:
        get_registry().unregister(executor_id)


@pytest.mark.asyncio
async def test_update_explicit_null_clears_stored_business_result(sync_db_session: Session) -> None:
    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="Nullable result", result={"old": 42})
    result = await TaskUpdateModule(repository).execute({"task_id": task.id, "result": None})
    sync_db_session.expire_all()
    stored = await repository.get_task_by_id(str(task.id))
    assert stored is not None and stored.result is None
    assert result["result"] is None


@pytest.mark.asyncio
async def test_delete_failure_is_reported_as_operation_error() -> None:
    repository = MagicMock()
    repository.get_task_by_id = AsyncMock(return_value=object())
    repository.delete_task = AsyncMock(return_value=False)
    with pytest.raises(ModuleError, match="delete"):
        await TaskDeleteModule(repository).execute({"task_id": "existing"})


def test_override_replaces_factory_and_category_index(registered_ids: list[str]) -> None:
    class OldExecutor(BaseTask):
        id = "review_override_regression"
        name = "Old executor"

        @property
        def type(self) -> str:
            return "review_old_type"

        async def execute(self, inputs: dict[str, Any]) -> Any:
            return "old"

    class NewExecutor(OldExecutor):
        name = "New executor"

        @property
        def type(self) -> str:
            return "review_new_type"

    registered_ids.append(OldExecutor.id)
    registry = get_registry()
    registry.register(
        OldExecutor(), executor_class=OldExecutor, factory=lambda inputs: OldExecutor()
    )
    registry.register(NewExecutor(), executor_class=NewExecutor, override=True)
    assert isinstance(registry.create_executor_instance(OldExecutor.id), NewExecutor)
    assert registry.get_by_type(ExtensionCategory.EXECUTOR, "review_old_type") is None
    assert [item.id for item in registry.list_executors()].count(OldExecutor.id) == 1


def test_child_hooks_do_not_mutate_parent_hooks(registered_ids: list[str]) -> None:
    async def parent_hook(*args: Any) -> None:
        return None

    async def child_hook(*args: Any) -> None:
        return None

    @executor_register(pre_hook=parent_hook)
    class ParentExecutor(BaseTask):
        id = "review_hook_parent_regression"
        name = "Parent"

        async def execute(self, inputs: dict[str, Any]) -> Any:
            return inputs

    @executor_register(pre_hook=child_hook)
    class ChildExecutor(ParentExecutor):
        id = "review_hook_child_regression"

    registered_ids.extend([ParentExecutor.id, ChildExecutor.id])
    assert getattr(ParentExecutor, "_executor_hooks")["pre_hook"] is parent_hook
    assert getattr(ChildExecutor, "_executor_hooks")["pre_hook"] is child_hook


def test_entry_point_class_executor_is_exposed_by_bridge(registered_ids: list[str]) -> None:
    executor_id = "review_entry_point_class"
    registered_ids.append(executor_id)

    def register_plugin() -> None:
        @executor_register()
        class PluginExecutor(BaseTask):
            id = executor_id
            name = "Plugin"
            description = "An entry-point class executor."

            async def execute(self, inputs: dict[str, Any]) -> Any:
                return inputs

    entry_point = MagicMock(name="class_plugin")
    entry_point.load.return_value = register_plugin
    with patch("importlib.metadata.entry_points", return_value=[entry_point]):
        adapters = discover_executor_modules()
    assert executor_id in {adapter.executor_id for adapter in adapters}


def test_runtime_decorated_class_is_exposed_without_bridge_changes(
    registered_ids: list[str],
) -> None:
    @executor_register()
    class RuntimeExecutor(BaseTask):
        id = "review_runtime_class"
        name = "Runtime executor"
        description = "A class registered after built-in discovery."

        async def execute(self, inputs: dict[str, Any]) -> Any:
            return inputs

    registered_ids.append(RuntimeExecutor.id)
    assert RuntimeExecutor.id in {adapter.executor_id for adapter in discover_executor_modules()}
