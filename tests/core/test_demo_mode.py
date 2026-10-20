"""
Test demo mode functionality (use_demo parameter)
"""

import pytest
from typing import TYPE_CHECKING, Dict, Any, Optional
from apflow.core.base import BaseTask
from apflow.core.execution.task_manager import TaskManager
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow import executor_register, clear_config
from apflow.core.extensions import get_registry


class TestDemoMode:
    """Test demo mode (use_demo) functionality"""

    def setup_method(self):
        """Clear config and registry before each test"""
        clear_config()
        registry = get_registry()
        registry._executor_classes.clear()
        registry._factory_functions.clear()

    @pytest.mark.asyncio
    async def test_demo_mode_default_result(self, sync_db_session):
        """Test that demo mode returns default result when executor doesn't implement get_demo_result"""

        @executor_register()
        class TestExecutor(BaseTask):
            id = "test_executor_no_demo"
            name = "Test Executor"
            description = "Test executor without get_demo_result"

            def __init__(self, inputs: Optional[Dict[str, Any]] = None) -> None:
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                return {"result": "actual_execution"}

            def get_input_schema(self):
                return {"type": "object"}

        # Register executor
        registry = get_registry()
        executor_instance = TestExecutor()
        registry = get_registry()
        registry.register(executor_instance, executor_class=TestExecutor, override=True)

        # Create task (use_demo is now passed as parameter to TaskManager, not in inputs)
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[], use_demo=True)
        task_repository = TaskRepository(sync_db_session)

        task = await task_repository.create_task(
            name="test_executor_no_demo",
            user_id="test_user",
            inputs={},
            schemas={"method": "test_executor_no_demo"},
        )

        # Execute task
        result = await task_manager._execute_task_with_schemas(task, task.inputs)
        result = result["result"]

        # Verify demo result
        assert result["demo_mode"] is True
        assert result["result"] == "Demo execution result"

    @pytest.mark.asyncio
    async def test_demo_mode_custom_result(self, sync_db_session):
        """Test that demo mode uses custom result when executor implements get_demo_result"""

        @executor_register()
        class TestExecutor(BaseTask):
            id = "test_executor_with_demo"
            name = "Test Executor"
            description = "Test executor with get_demo_result"

            def __init__(self, inputs: Optional[Dict[str, Any]] = None) -> None:
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                return {"result": "actual_execution"}

            def get_input_schema(self):
                return {"type": "object"}

            def get_demo_result(self, task, inputs):
                """Provide custom demo data"""
                return {
                    "status": "completed",
                    "result": "Custom demo result",
                    "data": {"demo": True, "task_id": task.id},
                }

        # Register executor
        registry = get_registry()
        executor_instance = TestExecutor()
        registry = get_registry()
        registry.register(executor_instance, executor_class=TestExecutor, override=True)

        # Create task (use_demo is now passed as parameter to TaskManager, not in inputs)
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[], use_demo=True)
        task_repository = TaskRepository(sync_db_session)

        task = await task_repository.create_task(
            name="test_executor_with_demo",
            user_id="test_user",
            inputs={"test_input": "value"},
            schemas={"method": "test_executor_with_demo"},
        )

        # Execute task
        result = await task_manager._execute_task_with_schemas(task, task.inputs)
        result = result["result"]

        # Verify custom demo result
        assert result["demo_mode"] is True
        assert result["status"] == "completed"
        assert result["result"] == "Custom demo result"
        assert result["data"]["demo"] is True
        assert result["data"]["task_id"] == task.id

    @pytest.mark.asyncio
    async def test_demo_mode_non_dict_result(self, sync_db_session):
        """Test that demo mode handles non-dict result from get_demo_result"""

        @executor_register()
        class TestExecutor(BaseTask):
            id = "test_executor_string_demo"
            name = "Test Executor"
            description = "Test executor returning string demo result"

            def __init__(self, inputs: Optional[Dict[str, Any]] = None) -> None:
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                return {"result": "actual_execution"}

            def get_input_schema(self):
                return {"type": "object"}

            def get_demo_result(self, task, inputs):
                """Return string instead of dict"""
                return "String demo result"

        # Register executor
        registry = get_registry()
        executor_instance = TestExecutor()
        registry = get_registry()
        registry.register(executor_instance, executor_class=TestExecutor, override=True)

        # Create task (use_demo is now passed as parameter to TaskManager, not in inputs)
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[], use_demo=True)
        task_repository = TaskRepository(sync_db_session)

        task = await task_repository.create_task(
            name="test_executor_string_demo",
            user_id="test_user",
            inputs={},
            schemas={"method": "test_executor_string_demo"},
        )

        # Execute task
        result = await task_manager._execute_task_with_schemas(task, task.inputs)
        result = result["result"]

        # Verify result format
        assert result == "String demo result"

    @pytest.mark.asyncio
    async def test_demo_mode_get_demo_result_exception(self, sync_db_session):
        """Test that demo mode falls back to default when get_demo_result raises exception"""

        @executor_register()
        class TestExecutor(BaseTask):
            id = "test_executor_demo_error"
            name = "Test Executor"
            description = "Test executor with get_demo_result that raises error"

            def __init__(self, inputs: Optional[Dict[str, Any]] = None) -> None:
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                return {"result": "actual_execution"}

            def get_input_schema(self):
                return {"type": "object"}

            def get_demo_result(self, task, inputs):
                """Raise exception"""
                raise ValueError("Demo result error")

        # Register executor
        registry = get_registry()
        executor_instance = TestExecutor()
        registry = get_registry()
        registry.register(executor_instance, executor_class=TestExecutor, override=True)

        # Create task (use_demo is now passed as parameter to TaskManager, not in inputs)
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[], use_demo=True)
        task_repository = TaskRepository(sync_db_session)

        task = await task_repository.create_task(
            name="test_executor_demo_error",
            user_id="test_user",
            inputs={},
            schemas={"method": "test_executor_demo_error"},
        )

        # Execute task (should fall back to default demo)
        result = await task_manager._execute_task_with_schemas(task, task.inputs)
        result = result["result"]

        # Verify fallback to default
        assert result["demo_mode"] is True
        assert result["result"] == "Demo execution result"

    @pytest.mark.asyncio
    async def test_demo_mode_normal_execution(self, sync_db_session):
        """Test that normal execution works when use_demo=False"""
        execute_called = []

        @executor_register()
        class TestExecutor(BaseTask):
            id = "test_executor_normal"
            name = "Test Executor"
            description = "Test executor for normal execution"

            def __init__(self, inputs: Optional[Dict[str, Any]] = None) -> None:
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                execute_called.append(inputs)
                return {"result": "actual_execution", "inputs": inputs}

            def get_input_schema(self):
                return {"type": "object"}

        # Register executor
        registry = get_registry()
        executor_instance = TestExecutor()
        registry = get_registry()
        registry.register(executor_instance, executor_class=TestExecutor, override=True)

        # Create task with use_demo=False (default)
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[], use_demo=False)
        task_repository = TaskRepository(sync_db_session)

        task = await task_repository.create_task(
            name="test_executor_normal",
            user_id="test_user",
            inputs={"test": "value"},
            schemas={"method": "test_executor_normal"},
        )

        # Execute task
        result = await task_manager._execute_task_with_schemas(task, task.inputs)
        result = result["result"]

        # Verify normal execution
        assert "demo_mode" not in result
        assert result["result"] == "actual_execution"
        assert len(execute_called) == 1
        assert execute_called[0]["test"] == "value"

    @pytest.mark.asyncio
    async def test_demo_mode_with_sleep_scale(self, sync_db_session):
        """Test demo mode with sleep scale factor"""
        import time
        from apflow.core.config import set_demo_sleep_scale, get_demo_sleep_scale

        @executor_register()
        class TestExecutor(BaseTask):
            id = "test_executor_sleep_scale"
            if not TYPE_CHECKING:
                type = "test"

            async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
                return {"result": "Real execution"}

            def get_input_schema(self) -> Dict[str, Any]:
                return {"type": "object"}

            def get_output_schema(self) -> Dict[str, Any]:
                return {"type": "object", "properties": {"result": {"type": "string"}}}

            def get_demo_result(
                self, task: Any, inputs: Dict[str, Any]
            ) -> Optional[Dict[str, Any]]:
                # Executor defines its own sleep time (2 seconds)
                return {
                    "result": "Demo execution result",
                    "demo_mode": True,
                    "_demo_sleep": 0.2,  # Executor wants 0.2s sleep
                }

        executor_instance = TestExecutor()
        registry = get_registry()
        registry.register(executor_instance, executor_class=TestExecutor, override=True)

        # Set global scale to 0.5 (half the executor's sleep time)
        set_demo_sleep_scale(0.5)
        assert get_demo_sleep_scale() == 0.5

        # Create task with demo mode
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[], use_demo=True)
        task_repository = TaskRepository(sync_db_session)

        task = await task_repository.create_task(
            name="test_executor_sleep_scale",
            user_id="test_user",
            inputs={},
            schemas={"method": "test_executor_sleep_scale"},
        )

        # Execute task and measure time
        start_time = time.time()
        result = await task_manager._execute_task_with_schemas(task, task.inputs)
        result = result["result"]
        elapsed_time = time.time() - start_time

        # Verify demo result
        assert result["demo_mode"] is True
        assert result["result"] == "Demo execution result"
        # _demo_sleep should be removed from result
        assert "_demo_sleep" not in result

        # Verify scaled sleep occurred (0.2 * 0.5 = 0.1 seconds)
        assert elapsed_time >= 0.1, f"Expected at least 0.1s sleep (0.2 * 0.5), got {elapsed_time}s"
        assert elapsed_time < 0.3, f"Expected less than 0.3s (scaled), got {elapsed_time}s"

        # Test with scale=2.0 (double the sleep time)
        set_demo_sleep_scale(2.0)
        start_time = time.time()
        result = await task_manager._execute_task_with_schemas(task, task.inputs)
        result = result["result"]
        elapsed_time = time.time() - start_time

        # Verify doubled sleep (0.2 * 2.0 = 0.4 seconds)
        assert (
            elapsed_time >= 0.3
        ), f"Expected at least 0.4s sleep (0.2 * 2.0),maybe less, got {elapsed_time}s"
        assert elapsed_time < 0.6, f"Expected less than 0.6s (scaled), got {elapsed_time}s"

        # Test with scale=0.0 (no sleep)
        set_demo_sleep_scale(0.0)
        start_time = time.time()
        result = await task_manager._execute_task_with_schemas(task, task.inputs)
        result = result["result"]
        elapsed_time = time.time() - start_time

        # Verify no sleep (should be very fast)
        assert elapsed_time < 0.1, f"Expected no sleep (<0.1s), got {elapsed_time}s"

        # Reset to default
        set_demo_sleep_scale(1.0)

    @pytest.mark.asyncio
    async def test_demo_mode_no_executor_sleep(self, sync_db_session):
        """Test demo mode when executor doesn't specify _demo_sleep (no sleep)"""
        import time
        from apflow.core.config import set_demo_sleep_scale

        @executor_register()
        class TestExecutor(BaseTask):
            id = "test_executor_no_sleep"
            if not TYPE_CHECKING:
                type = "test"

            async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
                return {"result": "Real execution"}

            def get_input_schema(self) -> Dict[str, Any]:
                return {"type": "object"}

            def get_output_schema(self) -> Dict[str, Any]:
                return {"type": "object", "properties": {"result": {"type": "string"}}}

            def get_demo_result(
                self, task: Any, inputs: Dict[str, Any]
            ) -> Optional[Dict[str, Any]]:
                # No _demo_sleep specified
                return {"result": "Demo execution result", "demo_mode": True}

        executor_instance = TestExecutor()
        registry = get_registry()
        registry.register(executor_instance, executor_class=TestExecutor, override=True)

        # Set global scale (should not matter if executor doesn't specify _demo_sleep)
        set_demo_sleep_scale(2.0)

        # Create task with demo mode
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[], use_demo=True)
        task_repository = TaskRepository(sync_db_session)

        task = await task_repository.create_task(
            name="test_executor_no_sleep",
            user_id="test_user",
            inputs={},
            schemas={"method": "test_executor_no_sleep"},
        )

        # Execute task and measure time
        start_time = time.time()
        result = await task_manager._execute_task_with_schemas(task, task.inputs)
        result = result["result"]
        elapsed_time = time.time() - start_time

        # Verify demo result
        assert result["demo_mode"] is True
        assert result["result"] == "Demo execution result"

        # Verify no sleep occurred (executor didn't specify _demo_sleep)
        assert elapsed_time < 0.1, f"Expected no sleep (<0.1s), got {elapsed_time}s"

        # Reset to default
        set_demo_sleep_scale(1.0)
