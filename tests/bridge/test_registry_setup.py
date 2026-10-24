"""Tests for registry_setup integration"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from typing import Any
from jsonschema.validators import validator_for
from apflow.bridge.registry_setup import create_apflow_registry
from apflow.bridge.module_adapter import ExecutableTaskModuleAdapter
from apflow.core.base import BaseTask
from apflow.core.extensions.decorators import executor_register

TASK_AND_SCHEDULE_MODULES = {
    "task.create",
    "task.create_tree",
    "task.execute",
    "task.submit",
    "task.receipt",
    "task.reconcile",
    "task.cancel",
    "task.get",
    "task.update",
    "task.list",
    "task.delete",
    "task.tree",
    "task.children",
    "task.link",
    "task.copy",
    "task.archive",
    "task.clone_mixed",
    "task.running",
    "task.scheduled",
    "schedule.set",
    "schedule.due",
    "schedule.trigger",
    "schedule.complete",
    "schedule.history",
    "schedule.export_ical",
}
BUILTIN_EXECUTORS = {
    "rest_executor",
    "send_email_executor",
    "aggregate_results_executor",
    "apflow_api_executor",
}


def _mock_dependencies():
    manager = MagicMock()
    creator = MagicMock()
    creator.create_task_trees_from_array = AsyncMock()
    repo = MagicMock()
    repo.list_tasks.return_value = []
    repo.count_tasks.return_value = 0
    return manager, creator, repo


def _colliding_adapter(executor_id: str) -> ExecutableTaskModuleAdapter:
    class DummyExecutor:
        pass

    return ExecutableTaskModuleAdapter(
        executor_class=DummyExecutor,
        executor_id=executor_id,
        executor_name="Colliding",
        executor_description="test",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )


class TestCreateApflowRegistry:
    def test_returns_populated_registry(self):
        manager, creator, repo = _mock_dependencies()
        registry = create_apflow_registry(manager, creator, repo)
        modules = list(registry.list())
        assert len(modules) > 5  # At least 5 task modules

    def test_task_modules_registered(self) -> None:
        manager, creator, repo = _mock_dependencies()
        registry = create_apflow_registry(manager, creator, repo)
        modules = list(registry.list())
        actual = {
            module.removeprefix("apflow.")
            for module in modules
            if module.startswith(("apflow.task.", "apflow.schedule."))
        }
        assert actual == TASK_AND_SCHEDULE_MODULES

    def test_executor_modules_registered(self) -> None:
        manager, creator, repo = _mock_dependencies()
        registry = create_apflow_registry(manager, creator, repo)
        modules = list(registry.list())
        assert {f"apflow.{executor}" for executor in BUILTIN_EXECUTORS}.issubset(modules)

    def test_every_registered_module_has_valid_input_and_output_schemas(self) -> None:
        manager, creator, repo = _mock_dependencies()
        registry = create_apflow_registry(manager, creator, repo)
        for module_id in registry.list():
            definition = registry.get_definition(module_id)
            assert definition is not None
            for schema in (definition.input_schema, definition.output_schema):
                assert isinstance(schema, dict) and schema, module_id
                assert schema.get("type") == "object", module_id
                validator_for(schema).check_schema(schema)

    def test_decorated_executor_automatically_appears_in_apcore_registry(self) -> None:
        @executor_register()
        class AcceptanceExecutor(BaseTask):
            id = "acceptance_decorated_executor"
            name = "Acceptance executor"
            description = "Verify automatic bridge registration"

            async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
                return {"received": inputs}

        manager, creator, repository = _mock_dependencies()
        registry = create_apflow_registry(manager, creator, repository)
        assert registry.has("apflow.acceptance_decorated_executor")

    def test_custom_namespace(self):
        manager, creator, repo = _mock_dependencies()
        registry = create_apflow_registry(manager, creator, repo, namespace="myapp")
        modules = list(registry.list())
        assert "myapp.task.create" in modules
        assert all(m.startswith("myapp.") for m in modules)

    def test_empty_namespace_raises(self):
        manager, creator, repo = _mock_dependencies()
        with pytest.raises(ValueError, match="non-empty"):
            create_apflow_registry(manager, creator, repo, namespace="")

    def test_builtin_module_collision_raises_instead_of_silently_disabling(self):
        """Regression: a discovered executor's id colliding with the built-in
        task.*/schedule.* namespace made the built-in module's registration
        fail — but the broad except-and-warn around it silently absorbed
        that failure, leaving the built-in capability missing from the
        registry with no signal to the operator. (Review CRITICAL #9)
        """
        manager, creator, repo = _mock_dependencies()
        with patch(
            "apflow.bridge.registry_setup.discover_executor_modules",
            return_value=[_colliding_adapter("task.create")],
        ):
            with pytest.raises(RuntimeError, match="task.create"):
                create_apflow_registry(manager, creator, repo)
