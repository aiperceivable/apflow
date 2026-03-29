"""
Test database session context management

This file tests the with_db_session_context function to ensure proper
session management and prevent async event loop issues.
"""

import pytest
import asyncio
from unittest.mock import patch, MagicMock
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession

from apflow.core.storage.context import (
    with_db_session_context,
    get_request_session,
    set_request_session,
    clear_request_session,
)
from apflow.core.storage.factory import (
    get_default_session,
    reset_default_session,
    reset_session_pool_manager,
)
from apflow.core.storage.sqlalchemy.models import Base, TaskModel
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


class TestWithDbSessionContext:
    """Test with_db_session_context async context manager"""

    def setup_method(self):
        """Reset state before each test"""
        reset_default_session()
        reset_session_pool_manager()
        clear_request_session()
        # Ensure Base.metadata is clean
        self._ensure_clean_metadata()

    def teardown_method(self):
        """Clean up after each test"""
        reset_default_session()
        reset_session_pool_manager()
        clear_request_session()

    def _ensure_clean_metadata(self):
        """Ensure Base.metadata is clean"""
        from apflow.core.storage.sqlalchemy.models import TASK_TABLE_NAME

        if TASK_TABLE_NAME in Base.metadata.tables:
            table = Base.metadata.tables[TASK_TABLE_NAME]
            # Remove any custom columns that might have been added in previous tests
            custom_columns = {"project_id", "priority_level", "department"}
            for col_name in custom_columns:
                if col_name in table.c:
                    table.c.remove(table.c[col_name])

    @pytest.mark.asyncio
    async def test_with_db_session_context_use_pool_true(self, tmp_path):
        """Test with_db_session_context with use_pool=True uses create_pooled_session"""
        db_path = tmp_path / "test.db"
        connection_string = f"sqlite:///{db_path}"

        # Create tables first
        from apflow.core.storage.factory import create_session

        test_session = create_session(connection_string=connection_string)
        Base.metadata.create_all(test_session.bind)
        test_session.close()

        # Verify that create_pooled_session is called when use_pool=True
        # Use AsyncMock for proper async context manager support
        from unittest.mock import AsyncMock

        with patch("apflow.core.storage.context.create_pooled_session") as mock_create_pooled:
            mock_session = MagicMock(spec=Session)
            mock_context = AsyncMock()
            mock_context.__aenter__.return_value = mock_session
            mock_create_pooled.return_value = mock_context

            async with with_db_session_context(use_pool=True) as session:
                # Verify create_pooled_session was called
                mock_create_pooled.assert_called_once()
                # Verify session is set in context
                assert get_request_session() == mock_session
                assert session == mock_session

            # Verify context manager exit was called
            assert mock_context.__aexit__.called

    @pytest.mark.asyncio
    async def test_with_db_session_context_use_pool_false(self, tmp_path, use_test_db_session):
        """Test with_db_session_context with use_pool=False uses get_default_session"""
        # use_test_db_session fixture sets up a test session
        default_session = get_default_session()

        # Verify that get_default_session is used when use_pool=False
        with patch("apflow.core.storage.context.get_default_session") as mock_get_default:
            mock_get_default.return_value = default_session

            async with with_db_session_context(use_pool=False) as session:
                # Verify get_default_session was called
                mock_get_default.assert_called_once()
                # Verify session is set in context
                assert get_request_session() == default_session
                assert session == default_session

    @pytest.mark.asyncio
    async def test_with_db_session_context_auto_commit(self, tmp_path, use_test_db_session):
        """Test with_db_session_context with auto_commit=True commits on success"""
        get_default_session()

        async with with_db_session_context(use_pool=False, auto_commit=True) as session:
            # Create a task to test commit
            repo = TaskRepository(session)
            await repo.create_task(
                id="test-task-1",
                name="Test Task",
                status="pending",
                user_id="test-user",
            )
            # Session should be committed automatically on exit

        # Verify task was committed (can be retrieved in a new session)
        new_session = get_default_session()
        repo = TaskRepository(new_session)
        retrieved_task = await repo.get_task_by_id("test-task-1")
        assert retrieved_task is not None
        assert retrieved_task.name == "Test Task"

    @pytest.mark.asyncio
    async def test_with_db_session_context_auto_rollback_on_error(self, tmp_path):
        """Test with_db_session_context with auto_commit=True calls rollback on error"""
        import uuid

        db_path = tmp_path / "test-rollback.db"
        connection_string = f"sqlite:///{db_path}"

        # Create tables first
        from apflow.core.storage.factory import create_session

        test_session = create_session(connection_string=connection_string)
        Base.metadata.create_all(test_session.bind)
        test_session.close()

        # Use pooled session to ensure proper isolation
        rollback_called = False

        try:
            async with with_db_session_context(use_pool=True, auto_commit=True) as session:
                # Patch rollback to track if it's called
                original_rollback = session.rollback
                if isinstance(session, AsyncSession):

                    async def tracked_rollback():
                        nonlocal rollback_called
                        rollback_called = True
                        await original_rollback()

                    session.rollback = tracked_rollback
                else:

                    def tracked_rollback():
                        nonlocal rollback_called
                        rollback_called = True
                        original_rollback()

                    session.rollback = tracked_rollback

                # Add a task object but don't commit
                task = TaskModel(
                    id=f"test-task-rollback-{uuid.uuid4().hex[:8]}",
                    name="Test Task",
                    status="pending",
                    user_id="test-user",
                )
                session.add(task)
                # Raise an error to trigger rollback
                raise ValueError("Test error")
        except ValueError:
            pass

        # Verify rollback was called
        assert rollback_called, "Rollback should be called when error occurs in context"

    @pytest.mark.asyncio
    async def test_with_db_session_context_preserves_old_session(self, tmp_path):
        """Test that with_db_session_context preserves and restores old session"""
        db_path = tmp_path / "test.db"
        connection_string = f"sqlite:///{db_path}"

        # Create tables first
        from apflow.core.storage.factory import create_session

        test_session = create_session(connection_string=connection_string)
        Base.metadata.create_all(test_session.bind)
        test_session.close()

        # Set an old session in context
        old_session = get_default_session()
        set_request_session(old_session)

        # Create a new session context using pooled session (different session)
        async with with_db_session_context(use_pool=True) as new_session:
            # Verify new session is in context
            assert get_request_session() == new_session
            # When using pooled session, it should be a different session
            # (unless old_session is also from pool, which is unlikely)
            assert new_session is not None

        # Verify old session is restored
        assert get_request_session() == old_session

    @pytest.mark.asyncio
    async def test_with_db_session_context_no_old_session(self, tmp_path, use_test_db_session):
        """Test that with_db_session_context clears context when no old session"""
        # Ensure no session in context
        clear_request_session()
        assert get_request_session() is None

        async with with_db_session_context(use_pool=False) as session:
            # Verify session is set
            assert get_request_session() == session

        # Verify context is cleared after exit
        assert get_request_session() is None

    @pytest.mark.asyncio
    async def test_with_db_session_context_event_loop_safety(self, tmp_path):
        """Test that with_db_session_context works correctly in async event loop"""
        import uuid

        db_path = tmp_path / "test-loop.db"
        connection_string = f"sqlite:///{db_path}"
        task_id = f"test-task-loop-{uuid.uuid4().hex[:8]}"

        # Create tables first
        from apflow.core.storage.factory import create_session

        test_session = create_session(connection_string=connection_string)
        Base.metadata.create_all(test_session.bind)
        test_session.close()

        # Test that pooled session is created in the current event loop
        # This prevents "Task got Future attached to a different loop" errors
        async def test_in_loop():
            async with with_db_session_context(use_pool=True) as session:
                # Verify session is valid
                assert session is not None
                # Verify we can use the session
                repo = TaskRepository(session)
                # This should not raise "Task got Future attached to a different loop"
                task = await repo.create_task(
                    id=task_id,
                    name="Test Task",
                    status="pending",
                    user_id="test-user",
                )
                return task

        # Run in async context
        result = await test_in_loop()
        assert result is not None
        assert result.id == task_id

    @pytest.mark.asyncio
    async def test_with_db_session_context_concurrent_usage(self, tmp_path):
        """Test that with_db_session_context works correctly with concurrent usage"""
        import uuid

        db_path = tmp_path / "test-concurrent.db"
        connection_string = f"sqlite:///{db_path}"

        # Create tables first
        from apflow.core.storage.factory import create_session

        test_session = create_session(connection_string=connection_string)
        Base.metadata.create_all(test_session.bind)
        test_session.close()

        async def create_task_in_context(task_id: str):
            async with with_db_session_context(use_pool=True) as session:
                repo = TaskRepository(session)
                task = await repo.create_task(
                    id=task_id,
                    name=f"Task {task_id}",
                    status="pending",
                    user_id="test-user",
                )
                return task

        # Create multiple tasks concurrently with unique IDs
        base_id = uuid.uuid4().hex[:8]
        task_ids = [f"task-{base_id}-{i}" for i in range(5)]
        tasks = await asyncio.gather(*[create_task_in_context(task_id) for task_id in task_ids])

        # Verify all tasks were created
        assert len(tasks) == 5
        for task in tasks:
            assert task is not None
            assert task.id in task_ids
