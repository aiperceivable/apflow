"""
Tests for task execution concurrency safety.

Covers:
- Atomic task claim (try_claim_for_execution) preventing double-execution
- Retry logic in execute_after_task preventing orphaned parent tasks
"""

import pytest
from unittest.mock import patch

from apflow.core.execution.task_manager import TaskManager


class TestTryClaimForExecution:
    """Test atomic compare-and-swap task claiming"""

    @pytest.mark.asyncio
    async def test_claim_pending_task_succeeds(self, sync_db_session):
        """A pending task can be claimed for execution"""
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])

        task = await task_manager.task_repository.create_task(
            name="test_task",
            user_id="test-user",
            status="pending",
        )

        claimed = await task_manager.task_repository.try_claim_for_execution(task.id)
        assert claimed is not None
        assert claimed.status == "in_progress"
        assert claimed.started_at is not None

    @pytest.mark.asyncio
    async def test_claim_already_in_progress_returns_none(self, sync_db_session):
        """A task already in_progress cannot be claimed again"""
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])

        task = await task_manager.task_repository.create_task(
            name="test_task",
            user_id="test-user",
            status="pending",
        )

        # First claim succeeds
        claimed1 = await task_manager.task_repository.try_claim_for_execution(task.id)
        assert claimed1 is not None

        # Second claim fails (task is now in_progress)
        claimed2 = await task_manager.task_repository.try_claim_for_execution(task.id)
        assert claimed2 is None

    @pytest.mark.asyncio
    async def test_claim_completed_task_with_reexecution(self, sync_db_session):
        """A completed task can be claimed when re-execution statuses are allowed"""
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])

        task = await task_manager.task_repository.create_task(
            name="test_task",
            user_id="test-user",
            status="pending",
        )
        await task_manager.task_repository.update_task(task_id=task.id, status="completed")

        # Default claim (only pending allowed) fails
        claimed = await task_manager.task_repository.try_claim_for_execution(task.id)
        assert claimed is None

        # Claim with re-execution statuses succeeds
        claimed = await task_manager.task_repository.try_claim_for_execution(
            task.id, allowed_statuses=["completed"]
        )
        assert claimed is not None
        assert claimed.status == "in_progress"

    @pytest.mark.asyncio
    async def test_claim_nonexistent_task_returns_none(self, sync_db_session):
        """Claiming a nonexistent task returns None (not an error)"""
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])

        claimed = await task_manager.task_repository.try_claim_for_execution("nonexistent-id")
        assert claimed is None

    @pytest.mark.asyncio
    async def test_claim_clears_error_field(self, sync_db_session):
        """Claiming a failed task clears the previous error"""
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])

        task = await task_manager.task_repository.create_task(
            name="test_task",
            user_id="test-user",
            status="pending",
        )
        await task_manager.task_repository.update_task(
            task_id=task.id, status="failed", error="previous error"
        )

        claimed = await task_manager.task_repository.try_claim_for_execution(
            task.id, allowed_statuses=["failed"]
        )
        assert claimed is not None
        assert claimed.error is None


class TestExecuteAfterTaskRetry:
    """Test retry logic for dependent task triggering"""

    @pytest.mark.asyncio
    async def test_retry_on_transient_failure(self, sync_db_session):
        """execute_after_task is retried on transient exceptions"""
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])

        task = await task_manager.task_repository.create_task(
            name="test_task",
            user_id="test-user",
            status="completed",
            result={"output": "data"},
        )

        call_count = 0

        async def flaky_execute_after_task(completed_task):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("transient DB error")
            # Third call succeeds

        with patch.object(
            task_manager,
            "execute_after_task",
            side_effect=flaky_execute_after_task,
        ):
            await task_manager._handle_task_execution_result(
                task, task.id, {"result": {"output": "data"}}
            )

        assert call_count == 3  # Called 3 times: 2 failures + 1 success

    @pytest.mark.asyncio
    async def test_exhausted_retries_logs_error(self, sync_db_session):
        """After all retries exhausted, error is logged (not raised)"""
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])

        task = await task_manager.task_repository.create_task(
            name="test_task",
            user_id="test-user",
            status="completed",
            result={"output": "data"},
        )

        with patch.object(
            task_manager,
            "execute_after_task",
            side_effect=ConnectionError("persistent DB error"),
        ):
            # Should not raise — error is caught and logged
            await task_manager._handle_task_execution_result(
                task, task.id, {"result": {"output": "data"}}
            )

    @pytest.mark.asyncio
    async def test_no_retry_on_success(self, sync_db_session):
        """No retry when execute_after_task succeeds on first attempt"""
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])

        task = await task_manager.task_repository.create_task(
            name="test_task",
            user_id="test-user",
            status="completed",
            result={"output": "data"},
        )

        call_count = 0

        async def successful_execute_after_task(completed_task):
            nonlocal call_count
            call_count += 1

        with patch.object(
            task_manager,
            "execute_after_task",
            side_effect=successful_execute_after_task,
        ):
            await task_manager._handle_task_execution_result(
                task, task.id, {"result": {"output": "data"}}
            )

        assert call_count == 1


class TestAtomicClaimInExecution:
    """Test that _execute_single_task uses atomic claim correctly"""

    @pytest.mark.asyncio
    async def test_execute_single_task_skips_already_claimed(self, sync_db_session):
        """_execute_single_task returns early if atomic claim fails"""
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])

        task = await task_manager.task_repository.create_task(
            name="test_task",
            user_id="test-user",
            status="in_progress",  # Already claimed
            schemas={"method": "aggregate_results_executor"},
        )

        # Should skip without error
        await task_manager._execute_single_task(task, use_callback=False)

        # Task should still be in_progress (not double-executed)
        refreshed = await task_manager.task_repository.get_task_by_id(task.id)
        assert refreshed is not None
        assert refreshed.status == "in_progress"

    @pytest.mark.asyncio
    async def test_task_start_callback_not_emitted_on_claim_failure(self, sync_db_session):
        """Streaming task_start callback is not emitted if claim fails"""
        task_manager = TaskManager(
            sync_db_session, pre_hooks=[], post_hooks=[], root_task_id="root"
        )

        task = await task_manager.task_repository.create_task(
            name="test_task",
            user_id="test-user",
            status="in_progress",  # Already claimed
            schemas={"method": "aggregate_results_executor"},
        )

        task_start_called = False

        original_task_start = task_manager.streaming_callbacks.task_start

        def tracking_task_start(task_id, **kwargs):
            nonlocal task_start_called
            task_start_called = True
            return original_task_start(task_id, **kwargs)

        task_manager.streaming_callbacks.task_start = tracking_task_start

        await task_manager._execute_single_task(task, use_callback=False)

        assert not task_start_called, "task_start should not be called when claim fails"
