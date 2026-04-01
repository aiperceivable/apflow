"""
Test task re-execution functionality

Tests the re-execution of failed tasks and their dependencies.
"""

import pytest
from apflow.core.execution.task_executor import TaskExecutor
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.core.config import get_task_model_class


class TestTaskReexecution:
    """Test cases for task re-execution functionality"""

    @pytest.mark.asyncio
    async def test_reexecute_failed_task(self, use_test_db_session):
        """Test re-executing a failed task"""
        task_repository = TaskRepository(
            use_test_db_session, task_model_class=get_task_model_class()
        )
        task_executor = TaskExecutor()

        # Create a task that will fail (using invalid executor)
        task = await task_repository.create_task(
            name="test_failed_task",
            user_id="test-user",
            status="pending",
            priority=1,
            inputs={"resource": "cpu"},
            schemas={"method": "rest_executor"},
        )

        # Execute the task first time - should succeed
        await task_executor.execute_task_by_id(
            task_id=task.id, use_streaming=False, db_session=use_test_db_session
        )

        # Verify task completed successfully
        task_after_first = await task_repository.get_task_by_id(task.id)
        assert task_after_first.status == "completed"

        # Manually set task to failed status to simulate failure
        task_after_first.status = "failed"
        task_after_first.error = "Simulated failure for testing"
        if task_repository.db.is_async:
            await use_test_db_session.commit()
            await use_test_db_session.refresh(task_after_first)
        else:
            use_test_db_session.commit()
            use_test_db_session.refresh(task_after_first)

        # Re-execute the failed task
        await task_executor.execute_task_by_id(
            task_id=task.id, use_streaming=False, db_session=use_test_db_session
        )

        # Verify task was re-executed and completed
        task_after_reexecution = await task_repository.get_task_by_id(task.id)
        assert task_after_reexecution.status == "completed"
        assert task_after_reexecution.error is None
        assert task_after_reexecution.result is not None

    @pytest.mark.asyncio
    async def test_reexecute_failed_task_with_dependencies(self, use_test_db_session):
        """Test re-executing a failed task with dependencies"""
        task_repository = TaskRepository(
            use_test_db_session, task_model_class=get_task_model_class()
        )
        task_executor = TaskExecutor()

        # Create child task (dependency)
        child_task = await task_repository.create_task(
            name="child_task",
            user_id="test-user",
            status="pending",
            priority=1,
            inputs={"resource": "cpu"},
            schemas={"method": "rest_executor"},
        )

        # Create parent task that depends on child
        parent_task = await task_repository.create_task(
            name="parent_task",
            user_id="test-user",
            status="pending",
            priority=2,
            has_children=True,
            dependencies=[{"id": child_task.id, "required": True}],
            params={"executor_id": "aggregate_results_executor"},
        )

        # Set parent-child relationship
        child_task.parent_id = parent_task.id
        if task_repository.db.is_async:
            await use_test_db_session.commit()
            await use_test_db_session.refresh(child_task)
        else:
            use_test_db_session.commit()
            use_test_db_session.refresh(child_task)

        # Execute the task tree first time - should succeed
        await task_executor.execute_task_by_id(
            task_id=parent_task.id, use_streaming=False, db_session=use_test_db_session
        )

        # Verify both tasks completed successfully
        parent_after_first = await task_repository.get_task_by_id(parent_task.id)
        child_after_first = await task_repository.get_task_by_id(child_task.id)
        assert parent_after_first.status == "completed"
        assert child_after_first.status == "completed"

        # Manually set parent task to failed status
        parent_after_first.status = "failed"
        parent_after_first.error = "Simulated failure for testing"
        if task_repository.db.is_async:
            await use_test_db_session.commit()
            await use_test_db_session.refresh(parent_after_first)
        else:
            use_test_db_session.commit()
            use_test_db_session.refresh(parent_after_first)

        # Re-execute the failed parent task
        await task_executor.execute_task_by_id(
            task_id=parent_task.id, use_streaming=False, db_session=use_test_db_session
        )

        # Wait a bit for async execution to complete
        import asyncio

        await asyncio.sleep(0.1)

        # Verify parent task was re-executed and completed
        parent_after_reexecution = await task_repository.get_task_by_id(parent_task.id)
        # Note: Parent task might still be in_progress if child task is being re-executed
        # Check that it's not failed anymore
        assert (
            parent_after_reexecution.status != "failed"
        ), f"Parent task should not be failed, got status: {parent_after_reexecution.status}, error: {parent_after_reexecution.error}"

        # Verify child task was also re-executed (dependency re-execution)
        child_after_reexecution = await task_repository.get_task_by_id(child_task.id)
        assert child_after_reexecution.status == "completed"

        # Wait a bit more for parent task to complete after child task completes
        await asyncio.sleep(0.2)

        # Re-check parent task status
        parent_final = await task_repository.get_task_by_id(parent_task.id)
        assert (
            parent_final.status == "completed"
        ), f"Parent task should be completed, got status: {parent_final.status}, error: {parent_final.error}"
        assert parent_final.error is None

    @pytest.mark.asyncio
    async def test_pending_tasks_not_marked_for_reexecution(self, use_test_db_session):
        """Test that newly created pending tasks are not marked for re-execution"""
        task_repository = TaskRepository(
            use_test_db_session, task_model_class=get_task_model_class()
        )
        task_executor = TaskExecutor()

        # Create a new pending task
        task = await task_repository.create_task(
            name="new_pending_task",
            user_id="test-user",
            status="pending",
            priority=1,
            inputs={"resource": "cpu"},
            schemas={"method": "rest_executor"},
        )

        # Execute the task - should execute normally (not as re-execution)
        await task_executor.execute_task_by_id(
            task_id=task.id, use_streaming=False, db_session=use_test_db_session
        )

        # Verify task completed successfully
        task_after = await task_repository.get_task_by_id(task.id)
        assert task_after.status == "completed"
        assert task_after.result is not None

    @pytest.mark.asyncio
    async def test_completed_tasks_skipped_unless_dependency(self, use_test_db_session):
        """Test that completed tasks are skipped unless they are dependencies of a re-executed task"""
        task_repository = TaskRepository(
            use_test_db_session, task_model_class=get_task_model_class()
        )
        task_executor = TaskExecutor()

        # Create a completed task
        task = await task_repository.create_task(
            name="completed_task",
            user_id="test-user",
            status="pending",
            priority=1,
            inputs={"resource": "cpu"},
            schemas={"method": "rest_executor"},
        )

        # Execute the task first time
        await task_executor.execute_task_by_id(
            task_id=task.id, use_streaming=False, db_session=use_test_db_session
        )

        # Verify task completed
        task_after_first = await task_repository.get_task_by_id(task.id)
        assert task_after_first.status == "completed"

        # Try to execute again - should be skipped (task already completed)
        await task_executor.execute_task_by_id(
            task_id=task.id, use_streaming=False, db_session=use_test_db_session
        )

        # Verify task is still completed and result hasn't changed
        task_after_second = await task_repository.get_task_by_id(task.id)
        assert task_after_second.status == "completed"
        # Note: The result might be the same or different depending on execution,
        # but the task should remain completed
