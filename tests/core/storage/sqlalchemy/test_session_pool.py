"""
Test Session Pool functionality for concurrent task tree execution

This file tests the SessionPoolManager and TaskTreeSession components
to ensure proper session management, limits, and cleanup.
"""

import pytest
import asyncio
import time
from unittest.mock import MagicMock
from sqlalchemy import text
from sqlalchemy.orm import Session

from apflow.core.storage.factory import (
    SessionPoolManager,
    create_task_tree_session,
    get_session_pool_manager,
    reset_session_pool_manager,
    SessionLimitExceeded,
)
from apflow.core.storage.sqlalchemy.models import Base, TaskModel
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


class TestSessionPoolManager:
    """Test SessionPoolManager basic functionality"""

    def setup_method(self):
        """Reset session pool manager before each test"""
        reset_session_pool_manager()

    def teardown_method(self):
        """Clean up after each test"""
        reset_session_pool_manager()

    def test_session_pool_manager_initialization(self):
        """Test SessionPoolManager can be initialized"""
        manager = SessionPoolManager()
        assert manager is not None
        assert manager.get_max_sessions() > 0
        assert manager.get_active_session_count() == 0

    def test_session_pool_manager_initialize_with_sqlite(self, tmp_path):
        """Test SessionPoolManager initialization with SQLite"""
        db_path = tmp_path / "test.db"
        manager = SessionPoolManager()
        manager.initialize(path=str(db_path))

        assert manager._engine is not None
        assert manager._sessionmaker is not None
        # _async_mode is set during initialization, check it's stored
        assert hasattr(manager, "_async_mode")

    def test_session_pool_manager_initialize_with_connection_string(self, tmp_path):
        """Test SessionPoolManager initialization with connection string"""
        db_path = tmp_path / "test.db"
        connection_string = f"sqlite:///{db_path}"

        manager = SessionPoolManager()
        manager.initialize(connection_string=connection_string)

        assert manager._engine is not None
        assert manager._sessionmaker is not None

    def test_create_and_release_session(self, tmp_path):
        """Test creating and releasing a session"""
        db_path = tmp_path / "test.db"
        manager = SessionPoolManager()
        manager.initialize(path=str(db_path))

        # Create session
        session = manager.create_session()
        assert session is not None
        assert isinstance(session, Session)
        assert manager.get_active_session_count() == 1

        # Release session
        manager.release_session(session)
        assert manager.get_active_session_count() == 0

    def test_session_limit_enforcement(self, tmp_path, monkeypatch):
        """Test that session limit is enforced"""
        # Set a low limit for testing
        monkeypatch.setenv("APFLOW_MAX_SESSIONS", "2")

        db_path = tmp_path / "test.db"
        manager = SessionPoolManager()
        manager.initialize(path=str(db_path))

        # Create sessions up to limit
        session1 = manager.create_session()
        session2 = manager.create_session()
        assert manager.get_active_session_count() == 2

        # Try to create one more - should raise SessionLimitExceeded
        with pytest.raises(SessionLimitExceeded):
            manager.create_session()

        # Release one session
        manager.release_session(session1)
        assert manager.get_active_session_count() == 1

        # Now should be able to create another
        session3 = manager.create_session()
        assert manager.get_active_session_count() == 2

        # Clean up
        manager.release_session(session2)
        manager.release_session(session3)

    def test_expired_session_cleanup(self, tmp_path, monkeypatch):
        """Test that expired sessions are cleaned up"""
        # Set a very short timeout for testing
        monkeypatch.setenv("APFLOW_SESSION_TIMEOUT", "1")

        db_path = tmp_path / "test.db"
        manager = SessionPoolManager()
        manager.initialize(path=str(db_path))

        # Create a session
        manager.create_session()
        assert manager.get_active_session_count() == 1

        # Wait for timeout
        time.sleep(1.5)

        # Try to create another session - should trigger cleanup
        session2 = manager.create_session()
        assert manager.get_active_session_count() == 1  # Expired session should be cleaned up

        # Clean up
        manager.release_session(session2)

    def test_concurrent_session_creation(self, tmp_path):
        """Test creating multiple sessions concurrently"""
        db_path = tmp_path / "test.db"
        manager = SessionPoolManager()
        manager.initialize(path=str(db_path))

        # Create multiple sessions
        sessions = []
        for _ in range(5):
            session = manager.create_session()
            sessions.append(session)

        assert manager.get_active_session_count() == 5

        # Release all sessions
        for session in sessions:
            manager.release_session(session)

        assert manager.get_active_session_count() == 0

    def test_get_session_pool_manager_singleton(self):
        """Test that get_session_pool_manager returns a singleton"""
        manager1 = get_session_pool_manager()
        manager2 = get_session_pool_manager()

        assert manager1 is manager2

    def test_reset_session_pool_manager(self):
        """Test that reset_session_pool_manager resets the singleton"""
        manager1 = get_session_pool_manager()
        reset_session_pool_manager()
        manager2 = get_session_pool_manager()

        # Should be different instances after reset
        assert manager1 is not manager2

    def test_release_session_close_before_untrack(self, tmp_path):
        """Test that session is still tracked if close() raises an exception"""
        db_path = tmp_path / "test.db"
        manager = SessionPoolManager()
        manager.initialize(path=str(db_path))

        # Create a real session first
        session = manager.create_session()
        assert manager.get_active_session_count() == 1

        # Replace close with a mock that raises
        original_close = session.close
        session.close = MagicMock(side_effect=RuntimeError("close failed"))

        # release_session should handle the error and still remove from tracking
        manager.release_session(session)

        # Session should be removed from tracking even if close failed
        # (the warning is logged but session is still cleaned up)
        assert manager.get_active_session_count() == 0

        # Restore original close for cleanup
        session.close = original_close


class TestTaskTreeSession:
    """Test TaskTreeSession context manager"""

    def setup_method(self):
        """Reset session pool manager before each test"""
        reset_session_pool_manager()

    def teardown_method(self):
        """Clean up after each test"""
        reset_session_pool_manager()

    @pytest.mark.asyncio
    async def test_task_tree_session_context_manager(self, tmp_path):
        """Test TaskTreeSession as context manager"""
        db_path = tmp_path / "test.db"
        connection_string = f"sqlite:///{db_path}"

        async with create_task_tree_session(connection_string=connection_string) as session:
            assert session is not None
            assert isinstance(session, Session)

            # Verify session is active in pool
            manager = get_session_pool_manager()
            assert manager.get_active_session_count() == 1

        # After context exit, session should be released
        manager = get_session_pool_manager()
        assert manager.get_active_session_count() == 0

    @pytest.mark.asyncio
    async def test_task_tree_session_with_default_path(self, tmp_path, monkeypatch):
        """Test TaskTreeSession with default path"""
        # Set a test database path
        test_db_path = tmp_path / "default.db"
        monkeypatch.setenv("APFLOW_DB_PATH", str(test_db_path))

        async with create_task_tree_session() as session:
            assert session is not None
            assert isinstance(session, Session)

    @pytest.mark.asyncio
    async def test_task_tree_session_error_handling(self, tmp_path, monkeypatch):
        """Test TaskTreeSession error handling for session limit"""
        # Set a very low limit
        monkeypatch.setenv("APFLOW_MAX_SESSIONS", "1")

        db_path = tmp_path / "test.db"
        connection_string = f"sqlite:///{db_path}"

        # Create first session
        async with create_task_tree_session(connection_string=connection_string) as session1:
            assert session1 is not None

            # Try to create second session - should raise SessionLimitExceeded
            with pytest.raises(SessionLimitExceeded):
                async with create_task_tree_session(connection_string=connection_string):
                    pass

    @pytest.mark.asyncio
    async def test_task_tree_session_exception_handling(self, tmp_path):
        """Test that TaskTreeSession properly releases session on exception"""
        db_path = tmp_path / "test.db"
        connection_string = f"sqlite:///{db_path}"

        try:
            async with create_task_tree_session(connection_string=connection_string) as session:
                assert session is not None
                # Raise an exception
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Session should still be released after exception
        manager = get_session_pool_manager()
        assert manager.get_active_session_count() == 0

    @pytest.mark.asyncio
    async def test_task_tree_session_database_operations(self, tmp_path):
        """Test that TaskTreeSession can perform database operations"""
        db_path = tmp_path / "test.db"
        connection_string = f"sqlite:///{db_path}"

        async with create_task_tree_session(connection_string=connection_string) as session:
            # Create tables
            Base.metadata.create_all(session.bind)

            # Perform a simple query
            result = session.execute(text("SELECT 1"))
            assert result.scalar() == 1


class TestSessionPoolWithTaskExecutor:
    """Test Session Pool integration with TaskExecutor"""

    def setup_method(self):
        """Reset session pool manager before each test"""
        reset_session_pool_manager()

    def teardown_method(self):
        """Clean up after each test"""
        reset_session_pool_manager()

    @pytest.mark.asyncio
    async def test_task_executor_with_task_tree_session(self, tmp_path):
        """Test TaskExecutor.execute_task_tree() with TaskTreeSession"""
        import uuid
        from apflow.core.execution.task_executor import TaskExecutor
        from apflow.core.types import TaskTreeNode
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        db_path = tmp_path / "test.db"
        connection_string = f"sqlite:///{db_path}"
        root_task_id = f"root-task-{uuid.uuid4().hex[:8]}"

        # Create tables and a task in the database
        # Re-import TaskModel to ensure we use the latest clean version
        from apflow.core.storage.sqlalchemy.models import TaskModel as CleanTaskModel

        async with create_task_tree_session(connection_string=connection_string) as session:
            Base.metadata.create_all(session.bind)
            repo = TaskRepository(session, task_model_class=CleanTaskModel)
            root_task = await repo.create_task(
                id=root_task_id,
                name="Root Task",
                status="pending",
                user_id="test-user",
                schemas={"method": "system_info_executor"},
                inputs={"resource": "cpu"},
            )
            session.commit()

        # Build task tree from database and execute
        async with create_task_tree_session(connection_string=connection_string) as session:
            repo = TaskRepository(session, task_model_class=TaskModel)
            root_task = await repo.get_task_by_id(root_task_id)
            task_tree = TaskTreeNode(task=root_task)

            executor = TaskExecutor()
            result = await executor.execute_task_tree(
                task_tree=task_tree, root_task_id=root_task_id, db_session=session
            )

            assert result is not None
            assert "status" in result or "root_task" in result

        # Verify session was released
        manager = get_session_pool_manager()
        assert manager.get_active_session_count() == 0

    @pytest.mark.asyncio
    async def test_concurrent_task_tree_execution(self, tmp_path):
        """Test concurrent execution of multiple task trees"""
        import uuid
        from apflow.core.execution.task_executor import TaskExecutor
        from apflow.core.types import TaskTreeNode

        db_path = tmp_path / "test.db"
        connection_string = f"sqlite:///{db_path}"

        # Create unique task IDs
        task_ids = [f"root-task-{uuid.uuid4().hex[:8]}" for _ in range(3)]

        # Create tables and tasks in the database
        async with create_task_tree_session(connection_string=connection_string) as session:
            Base.metadata.create_all(session.bind)
            repo = TaskRepository(session, task_model_class=TaskModel)
            for i, task_id in enumerate(task_ids):
                await repo.create_task(
                    id=task_id,
                    name=f"Root Task {i}",
                    status="pending",
                    user_id="test-user",
                    schemas={"method": "system_info_executor"},
                    inputs={"resource": "cpu"},
                )
            session.commit()

        # Build task trees from database
        task_trees = []
        async with create_task_tree_session(connection_string=connection_string) as session:
            repo = TaskRepository(session, task_model_class=TaskModel)
            for task_id in task_ids:
                root_task = await repo.get_task_by_id(task_id)
                task_tree = TaskTreeNode(task=root_task)
                task_trees.append((task_tree, task_id))

        # Execute concurrently
        async def execute_tree(task_tree, root_task_id):
            async with create_task_tree_session(connection_string=connection_string) as session:
                executor = TaskExecutor()
                result = await executor.execute_task_tree(
                    task_tree=task_tree, root_task_id=root_task_id, db_session=session
                )
                return result

        results = await asyncio.gather(
            *[execute_tree(tree, root_id) for tree, root_id in task_trees]
        )

        # Verify all executions completed
        assert len(results) == 3
        for result in results:
            assert result is not None

        # Verify all sessions were released
        manager = get_session_pool_manager()
        assert manager.get_active_session_count() == 0

    @pytest.mark.asyncio
    async def test_session_isolation_between_task_trees(self, tmp_path):
        """Test that different task trees use isolated sessions"""
        import uuid
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        db_path = tmp_path / "test.db"
        connection_string = f"sqlite:///{db_path}"
        task1_id = f"task-1-{uuid.uuid4().hex[:8]}"
        task2_id = f"task-2-{uuid.uuid4().hex[:8]}"

        # Create tables
        async with create_task_tree_session(connection_string=connection_string) as session:
            Base.metadata.create_all(session.bind)

        # Create two tasks in different sessions
        async with create_task_tree_session(connection_string=connection_string) as session1:
            repo1 = TaskRepository(session1, task_model_class=TaskModel)
            await repo1.create_task(id=task1_id, name="Task 1", user_id="user-1")
            session1.commit()

        async with create_task_tree_session(connection_string=connection_string) as session2:
            repo2 = TaskRepository(session2, task_model_class=TaskModel)
            await repo2.create_task(id=task2_id, name="Task 2", user_id="user-2")
            session2.commit()

            # Verify task1 is visible in session2 (committed tasks are visible across sessions)
            retrieved_task1 = await repo2.get_task_by_id(task1_id)
            assert retrieved_task1 is not None
            assert retrieved_task1.id == task1_id

            # Verify task2 is visible
            retrieved_task2 = await repo2.get_task_by_id(task2_id)
            assert retrieved_task2 is not None
            assert retrieved_task2.id == task2_id

        # Verify sessions were released
        manager = get_session_pool_manager()
        assert manager.get_active_session_count() == 0

    @pytest.mark.asyncio
    async def test_concurrent_execution_with_session_limit(self, tmp_path, monkeypatch):
        """Test concurrent execution respects session limit"""
        import uuid
        from apflow.core.execution.task_executor import TaskExecutor
        from apflow.core.types import TaskTreeNode

        # Set a low session limit
        monkeypatch.setenv("APFLOW_MAX_SESSIONS", "2")

        db_path = tmp_path / "test.db"
        connection_string = f"sqlite:///{db_path}"

        # Create unique task IDs
        task_ids = [f"root-task-{uuid.uuid4().hex[:8]}" for _ in range(5)]

        # Create tables and tasks in the database
        async with create_task_tree_session(connection_string=connection_string) as session:
            Base.metadata.create_all(session.bind)
            repo = TaskRepository(session, task_model_class=TaskModel)
            for i, task_id in enumerate(task_ids):
                await repo.create_task(
                    id=task_id,
                    name=f"Root Task {i}",
                    status="pending",
                    user_id="test-user",
                    schemas={"method": "system_info_executor"},
                    inputs={"resource": "cpu"},
                )
            session.commit()

        # Try to execute all tasks concurrently - some should hit session limit
        async def execute_tree(task_id):
            try:
                async with create_task_tree_session(connection_string=connection_string) as session:
                    repo = TaskRepository(session, task_model_class=TaskModel)
                    root_task = await repo.get_task_by_id(task_id)
                    if root_task is None:
                        return None
                    task_tree = TaskTreeNode(task=root_task)
                    executor = TaskExecutor()
                    result = await executor.execute_task_tree(
                        task_tree=task_tree, root_task_id=task_id, db_session=session
                    )
                    return result
            except SessionLimitExceeded:
                # Expected when limit is reached
                return "limit_exceeded"

        # Execute concurrently - with limit of 2, some will wait
        results = await asyncio.gather(
            *[execute_tree(task_id) for task_id in task_ids], return_exceptions=True
        )

        # Verify some executions completed (not all may succeed due to limit)
        completed = [
            r
            for r in results
            if r is not None and r != "limit_exceeded" and not isinstance(r, Exception)
        ]
        assert len(completed) > 0, "At least some tasks should have completed"

        # Verify all sessions were released
        manager = get_session_pool_manager()
        assert manager.get_active_session_count() == 0

    @pytest.mark.asyncio
    async def test_concurrent_execution_no_data_conflicts(self, tmp_path):
        """Test that concurrent executions don't cause data conflicts"""
        import uuid
        from apflow.core.execution.task_executor import TaskExecutor
        from apflow.core.types import TaskTreeNode
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        # Use default TaskModel - setup_method already ensures clean metadata
        from apflow.core.storage.sqlalchemy.models import (
            TaskModel as CleanTaskModel,
            Base as CleanBase,
        )

        db_path = tmp_path / "test.db"
        connection_string = f"sqlite:///{db_path}"

        # Create unique task IDs
        task_ids = [f"root-task-{uuid.uuid4().hex[:8]}" for _ in range(3)]

        # Create tables and tasks in the database
        async with create_task_tree_session(connection_string=connection_string) as session:
            CleanBase.metadata.create_all(session.bind)
            repo = TaskRepository(session, task_model_class=CleanTaskModel)
            for i, task_id in enumerate(task_ids):
                await repo.create_task(
                    id=task_id,
                    name=f"Root Task {i}",
                    status="pending",
                    user_id=f"user-{i}",
                    schemas={"method": "system_info_executor"},
                    inputs={"resource": "cpu"},
                )
            session.commit()

        # Execute all tasks concurrently
        async def execute_tree(task_id):
            async with create_task_tree_session(connection_string=connection_string) as session:
                repo = TaskRepository(session, task_model_class=CleanTaskModel)
                root_task = await repo.get_task_by_id(task_id)
                task_tree = TaskTreeNode(task=root_task)
                executor = TaskExecutor()
                result = await executor.execute_task_tree(
                    task_tree=task_tree, root_task_id=task_id, db_session=session
                )
                return result

        results = await asyncio.gather(*[execute_tree(task_id) for task_id in task_ids])

        # Verify all executions completed successfully
        assert len(results) == 3
        for result in results:
            assert result is not None

        # Verify all tasks are in completed state (no conflicts)
        async with create_task_tree_session(connection_string=connection_string) as session:
            repo = TaskRepository(session, task_model_class=CleanTaskModel)
            for task_id in task_ids:
                task = await repo.get_task_by_id(task_id)
                assert task is not None
                assert task.status == "completed"

        # Verify all sessions were released
        manager = get_session_pool_manager()
        assert manager.get_active_session_count() == 0

    @pytest.mark.asyncio
    async def test_session_cleanup_after_execution(self, tmp_path):
        """Test that sessions are properly cleaned up after execution"""
        import uuid
        from apflow.core.execution.task_executor import TaskExecutor
        from apflow.core.types import TaskTreeNode

        db_path = tmp_path / "test.db"
        connection_string = f"sqlite:///{db_path}"
        root_task_id = f"root-task-cleanup-{uuid.uuid4().hex[:8]}"

        manager = get_session_pool_manager()
        initial_count = manager.get_active_session_count()

        # Create tables and a task in the database
        # Re-import TaskModel to ensure we use the latest clean version
        from apflow.core.storage.sqlalchemy.models import TaskModel as CleanTaskModel

        async with create_task_tree_session(connection_string=connection_string) as session:
            Base.metadata.create_all(session.bind)
            repo = TaskRepository(session, task_model_class=CleanTaskModel)
            await repo.create_task(
                id=root_task_id,
                name="Root Task",
                status="pending",
                user_id="test-user",
                schemas={"method": "system_info_executor"},
                inputs={"resource": "cpu"},
            )
            session.commit()

        # Build task tree from database
        async with create_task_tree_session(connection_string=connection_string) as session:
            repo = TaskRepository(session, task_model_class=TaskModel)
            root_task = await repo.get_task_by_id(root_task_id)
            task_tree = TaskTreeNode(task=root_task)

        async with create_task_tree_session(connection_string=connection_string) as session:
            executor = TaskExecutor()
            await executor.execute_task_tree(
                task_tree=task_tree, root_task_id=root_task_id, db_session=session
            )
            # Session should still be active here
            assert manager.get_active_session_count() == initial_count + 1

        # After context exit, session should be released
        assert manager.get_active_session_count() == initial_count
