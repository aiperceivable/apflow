"""Tests for registry_setup integration"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from apflow.bridge.registry_setup import create_apflow_registry
from apflow.bridge.module_adapter import ExecutableTaskModuleAdapter


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

    def test_task_modules_registered(self):
        manager, creator, repo = _mock_dependencies()
        registry = create_apflow_registry(manager, creator, repo)
        modules = list(registry.list())
        assert "apflow.task.create" in modules
        assert "apflow.task.execute" in modules
        assert "apflow.task.list" in modules
        assert "apflow.task.get" in modules
        assert "apflow.task.delete" in modules

    def test_executor_modules_registered(self):
        manager, creator, repo = _mock_dependencies()
        registry = create_apflow_registry(manager, creator, repo)
        modules = list(registry.list())
        # At least one executor (e.g., rest_executor) should be registered
        executor_modules = [m for m in modules if not m.startswith("apflow.task.")]
        assert len(executor_modules) > 0

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
