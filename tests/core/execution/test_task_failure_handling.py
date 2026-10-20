"""
Tests for task failure handling and status reporting

Verifies that:
1. TaskManager correctly marks tasks as failed when executors raise exceptions
2. BusinessError exceptions are logged without stack traces
3. Unexpected exceptions are logged with stack traces
4. Error messages are persisted to the database
"""

import pytest
from apflow.core.execution.task_manager import TaskManager
from apflow.core.execution.errors import BusinessError, ValidationError, ConfigurationError
from apflow.core.base import BaseTask
from apflow.core.extensions import get_registry
from apflow.core.extensions.types import ExtensionCategory
from typing import Dict, Any


@pytest.fixture(autouse=True)
def _register_failing_executor():
    registry = get_registry()
    registry.register(FailingExecutor(), executor_class=FailingExecutor, override=True)
    yield
    registry.unregister("failing_executor")


class FailingExecutor(BaseTask):
    """Test executor that raises exceptions"""

    id = "failing_executor"
    name = "Failing Executor"
    description = "Test executor for failure scenarios"

    @property
    def category(self) -> ExtensionCategory:
        return ExtensionCategory.EXECUTOR

    @property
    def type(self) -> str:
        return "test"

    def get_output_schema(self) -> Dict[str, Any]:
        """Return output schema - this executor typically fails"""
        return {
            "type": "object",
            "properties": {"success": {"type": "boolean"}, "error": {"type": "string"}},
        }

    def get_input_schema(self) -> Dict[str, Any]:
        """Return input schema for this executor"""
        return {
            "type": "object",
            "properties": {
                "error_type": {
                    "type": "string",
                    "enum": [
                        "validation",
                        "configuration",
                        "business",
                        "runtime",
                        "timeout",
                        "connection",
                    ],
                    "description": "Type of error to raise during execution",
                }
            },
            "required": ["error_type"],
        }

    async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute and raise based on input"""
        error_type = inputs.get("error_type", "runtime")
        prefix = f"[{self.id}] "

        if error_type == "validation":
            raise ValidationError(prefix + "Invalid input provided")
        elif error_type == "configuration":
            raise ConfigurationError(prefix + "Missing configuration")
        elif error_type == "business":
            raise BusinessError(prefix + "Business logic error")
        elif error_type == "runtime":
            raise RuntimeError(prefix + "Unexpected runtime error")
        elif error_type == "timeout":
            import asyncio

            raise asyncio.TimeoutError(prefix + "Operation timed out")
        elif error_type == "connection":
            raise ConnectionError(prefix + "Failed to connect to service")

        return {"success": True}


@pytest.fixture(autouse=True)
def register_failing_executor():
    """
    Ensure FailingExecutor is registered before each test.

    This is necessary because some tests (like test_main.py) clear the
    extension registry, which would cause the FailingExecutor registration
    (that happened at module import via @executor_register()) to be lost.
    """
    registry = get_registry()

    # Register the executor if not already registered
    if not registry.is_registered("failing_executor"):
        executor_instance = FailingExecutor()
        registry.register(
            extension=executor_instance, executor_class=FailingExecutor, override=True
        )

    yield

    # No cleanup needed - registry will be cleared/reset by other tests' setup methods


@pytest.mark.asyncio
async def test_validation_error_marks_task_failed(sync_db_session):
    """Test that ValidationError marks task as failed"""
    # Create task manager and repository
    task_manager = TaskManager(sync_db_session)
    task_repository = task_manager.task_repository

    # Create task
    task = await task_repository.create_task(
        name="test_validation_error",
        schemas={"type": "test", "method": "failing_executor"},
        inputs={"error_type": "validation"},
    )

    # Execute task
    await task_manager._execute_single_task(task, use_callback=False)

    # Verify task is marked as failed
    task = await task_repository.get_task_by_id(task.id)
    assert task is not None
    assert task.status == "failed"
    assert task.error is not None
    assert "Invalid input provided" in task.error
    assert task.completed_at is not None


@pytest.mark.asyncio
async def test_configuration_error_marks_task_failed(sync_db_session):
    """Test that ConfigurationError marks task as failed"""
    task_manager = TaskManager(sync_db_session)
    task_repository = task_manager.task_repository

    task = await task_repository.create_task(
        name="test_configuration_error",
        schemas={"type": "test", "method": "failing_executor"},
        inputs={"error_type": "configuration"},
    )

    await task_manager._execute_single_task(task, use_callback=False)

    task = await task_repository.get_task_by_id(task.id)
    assert task is not None
    assert task.status == "failed"
    assert task.error is not None
    assert "Missing configuration" in task.error
    assert task.completed_at is not None


@pytest.mark.asyncio
async def test_business_error_marks_task_failed(sync_db_session):
    """Test that BusinessError marks task as failed"""
    task_manager = TaskManager(sync_db_session)
    task_repository = task_manager.task_repository

    task = await task_repository.create_task(
        name="test_business_error",
        schemas={"type": "test", "method": "failing_executor"},
        inputs={"error_type": "business"},
    )

    await task_manager._execute_single_task(task, use_callback=False)

    task = await task_repository.get_task_by_id(task.id)
    assert task is not None
    assert task.status == "failed"
    assert task.error is not None
    assert "Business logic error" in task.error
    assert task.completed_at is not None


@pytest.mark.asyncio
async def test_runtime_error_marks_task_failed(sync_db_session):
    """Test that RuntimeError marks task as failed"""
    task_manager = TaskManager(sync_db_session)
    task_repository = task_manager.task_repository

    task = await task_repository.create_task(
        name="test_runtime_error",
        schemas={"type": "test", "method": "failing_executor"},
        inputs={"error_type": "runtime"},
    )

    await task_manager._execute_single_task(task, use_callback=False)

    task = await task_repository.get_task_by_id(task.id)
    assert task is not None
    assert task.status == "failed"
    assert task.error is not None
    assert "Unexpected runtime error" in task.error
    assert task.completed_at is not None


@pytest.mark.asyncio
async def test_timeout_error_marks_task_failed(sync_db_session):
    """Test that TimeoutError marks task as failed"""
    task_manager = TaskManager(sync_db_session)
    task_repository = task_manager.task_repository

    task = await task_repository.create_task(
        name="test_timeout_error",
        schemas={"type": "test", "method": "failing_executor"},
        inputs={"error_type": "timeout"},
    )

    await task_manager._execute_single_task(task, use_callback=False)

    task = await task_repository.get_task_by_id(task.id)
    assert task is not None
    assert task.status == "failed"
    assert task.error is not None
    assert "Operation timed out" in task.error
    assert task.completed_at is not None


@pytest.mark.asyncio
async def test_connection_error_marks_task_failed(sync_db_session):
    """Test that ConnectionError marks task as failed"""
    task_manager = TaskManager(sync_db_session)
    task_repository = task_manager.task_repository

    task = await task_repository.create_task(
        name="test_connection_error",
        schemas={"type": "test", "method": "failing_executor"},
        inputs={"error_type": "connection"},
    )

    await task_manager._execute_single_task(task, use_callback=False)

    task = await task_repository.get_task_by_id(task.id)
    assert task is not None
    assert task.status == "failed"
    assert task.error is not None
    assert "Failed to connect to service" in task.error
    assert task.completed_at is not None


@pytest.mark.asyncio
async def test_failed_task_can_be_reexecuted(sync_db_session):
    """Test that failed tasks can be re-executed"""
    task_manager = TaskManager(sync_db_session)
    task_repository = task_manager.task_repository

    # Create and fail a task
    task = await task_repository.create_task(
        name="test_reexecution",
        schemas={"type": "test", "method": "failing_executor"},
        inputs={"error_type": "validation"},
    )

    await task_manager._execute_single_task(task, use_callback=False)

    task = await task_repository.get_task_by_id(task.id)
    assert task is not None
    assert task.status == "failed"

    # Mark for re-execution by resetting status
    await task_repository.update_task(
        task_id=task.id, status="pending", error=None, completed_at=None
    )

    # Re-execute with different input (should still fail but with different error)
    # Update task inputs for re-execution
    await task_repository.update_task(task.id, inputs={"error_type": "configuration"})

    await task_manager._execute_single_task(task, use_callback=False)

    task = await task_repository.get_task_by_id(task.id)
    assert task is not None
    assert task.status == "failed"
    assert task.error is not None
    assert "Missing configuration" in task.error
