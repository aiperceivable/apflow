"""
Test TaskExecutor concurrent execution protection

These tests verify that:
1. TaskExecutor prevents concurrent execution of the same task tree
2. The protection works at the executor level (not just API layer)
"""

import pytest

from apflow.core.execution.task_executor import TaskExecutor
from apflow.core.types import TaskTreeNode
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


class TestTaskExecutorConcurrentProtection:
    """Test TaskExecutor concurrent execution protection"""

    @pytest.mark.asyncio
    async def test_execute_task_tree_rejects_already_running(self, sync_db_session):
        """
        Test that execute_task_tree returns already_running status
        when the same task tree is already being executed
        """
        # Create a task
        repo = TaskRepository(sync_db_session)
        task = await repo.create_task(
            name="Test Task",
            user_id="test-user",
            schemas={"method": "rest_executor"},
            inputs={"resource": "cpu"},
        )
        task_tree = TaskTreeNode(task)
        root_task_id = str(task.id)

        # Create TaskExecutor
        task_executor = TaskExecutor()

        # Manually mark the task as running in TaskTracker
        await task_executor.task_tracker.start_task_tracking(root_task_id)

        try:
            # Attempt to execute the same task tree
            result = await task_executor.execute_task_tree(
                task_tree=task_tree,
                root_task_id=root_task_id,
                db_session=sync_db_session,
            )

            # Verify the result indicates already running
            assert result["status"] == "already_running"
            assert result["root_task_id"] == root_task_id
            assert "already running" in result["message"].lower()

        finally:
            # Clean up: stop tracking
            await task_executor.task_tracker.stop_task_tracking(root_task_id)

    @pytest.mark.asyncio
    async def test_execute_task_tree_allows_different_tasks(self, sync_db_session):
        """
        Test that execute_task_tree allows execution of different task trees
        """
        # Create two different tasks
        repo = TaskRepository(sync_db_session)
        task1 = await repo.create_task(
            name="Test Task 1",
            user_id="test-user",
            schemas={"method": "rest_executor"},
            inputs={"resource": "cpu"},
        )
        task2 = await repo.create_task(
            name="Test Task 2",
            user_id="test-user",
            schemas={"method": "rest_executor"},
            inputs={"resource": "memory"},
        )

        task_tree2 = TaskTreeNode(task2)

        # Create TaskExecutor
        task_executor = TaskExecutor()

        # Mark task1 as running (simulating task1 is being executed)
        await task_executor.task_tracker.start_task_tracking(str(task1.id))

        try:
            # Task2 should still be able to execute
            result = await task_executor.execute_task_tree(
                task_tree=task_tree2,
                root_task_id=str(task2.id),
                db_session=sync_db_session,
            )

            # Task2 should complete successfully (not blocked by task1)
            assert result["status"] != "already_running"
            assert result["root_task_id"] == task2.id

        finally:
            # Clean up
            await task_executor.task_tracker.stop_task_tracking(str(task1.id))
            await task_executor.task_tracker.stop_task_tracking(str(task2.id))

    @pytest.mark.asyncio
    async def test_concurrent_execution_same_task_blocked(self, sync_db_session):
        """
        Test that concurrent calls to execute_task_tree for the same task
        result in only one execution proceeding
        """
        # Create a task with a slow executor (simulated)
        repo = TaskRepository(sync_db_session)
        task = await repo.create_task(
            name="Slow Task",
            user_id="test-user",
            schemas={"method": "rest_executor"},
            inputs={"resource": "cpu"},
        )
        task_tree = TaskTreeNode(task)
        root_task_id = str(task.id)

        # Create TaskExecutor
        task_executor = TaskExecutor()

        # Track execution results
        results = []

        async def execute_with_delay():
            """Execute and record result"""
            result = await task_executor.execute_task_tree(
                task_tree=task_tree,
                root_task_id=root_task_id,
                db_session=sync_db_session,
            )
            results.append(result)
            return result

        # Start first execution
        first_result = await execute_with_delay()

        # The first execution should complete normally
        assert first_result["status"] in ["completed", "failed", "pending"]

        # Now manually mark as running and try again
        await task_executor.task_tracker.start_task_tracking(root_task_id)

        try:
            second_result = await execute_with_delay()
            # Second attempt should be blocked
            assert second_result["status"] == "already_running"
        finally:
            await task_executor.task_tracker.stop_task_tracking(root_task_id)

    @pytest.mark.asyncio
    async def test_task_tracker_cleanup_after_execution(self, sync_db_session):
        """
        Test that TaskTracker properly cleans up after execution completes
        """
        repo = TaskRepository(sync_db_session)
        task = await repo.create_task(
            name="Test Task",
            user_id="test-user",
            schemas={"method": "rest_executor"},
            inputs={"resource": "cpu"},
        )
        task_tree = TaskTreeNode(task)
        root_task_id = str(task.id)

        task_executor = TaskExecutor()

        # Verify task is not running before execution
        assert not task_executor.is_task_running(root_task_id)

        # Execute task
        await task_executor.execute_task_tree(
            task_tree=task_tree,
            root_task_id=root_task_id,
            db_session=sync_db_session,
        )

        # Verify task is not running after execution completes
        assert not task_executor.is_task_running(root_task_id)

        # Tracking is cleaned up, but execution history cannot be reset in place.
        from apcore.errors import ModuleError

        with pytest.raises(ModuleError, match="new run"):
            await task_executor.execute_task_tree(
                task_tree=task_tree,
                root_task_id=root_task_id,
                db_session=sync_db_session,
            )
        assert not task_executor.is_task_running(root_task_id)
