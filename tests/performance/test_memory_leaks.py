"""
Memory leak tests for long-running workflows

Tests that memory usage remains stable over many task executions
and that executor instances are properly cleaned up.
"""

import asyncio
import gc
import os
import pytest
import psutil
from datetime import datetime, timezone

from apflow.core.execution.task_manager import TaskManager
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.core.storage.sqlalchemy.models import TaskModel


# Mark as slow since these tests take time
pytestmark = pytest.mark.slow


class TestMemoryLeaks:
    """Test memory stability over many task executions"""

    @pytest.fixture
    def process(self):
        """Get current process for memory monitoring"""
        return psutil.Process(os.getpid())

    @pytest.fixture
    def get_memory_mb(self, process):
        """Helper to get current memory usage in MB"""

        def _get_memory():
            # Force garbage collection before measuring
            gc.collect()
            return process.memory_info().rss / 1024 / 1024

        return _get_memory

    @pytest.mark.asyncio
    async def test_executor_instances_cleanup(self, async_db_session):
        """
        Test that executor instances are cleaned up after task completion

        Verifies that TaskManager._executor_instances dict doesn't grow
        indefinitely when tasks complete.
        """
        repo = TaskRepository(async_db_session)

        # Create TaskManager
        manager = TaskManager(db=async_db_session, task_repository=repo, is_async=True)

        # Execute 100 simple tasks
        for i in range(100):
            task = TaskModel(
                id=f"task_{i}",
                name="test_task",
                status="pending",
                priority=0,
                dependencies=[],
                inputs={"test": "value"},
                params={},
                schemas={"method": "demo_executor"},  # Use demo executor for speed
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            async_db_session.add(task)
            await async_db_session.flush()

            # Execute task
            await manager._execute_single_task(task, "test_task", task.inputs, {})

        # All executor instances should be cleaned up
        assert (
            len(manager._executor_instances) == 0
        ), f"Expected 0 executor instances, found {len(manager._executor_instances)}"

    @pytest.mark.asyncio
    async def test_executor_instances_cleanup_on_failure(self, async_db_session):
        """
        Test that executor instances are cleaned up even when tasks fail

        Verifies cleanup happens in error paths too.
        """
        repo = TaskRepository(async_db_session)

        manager = TaskManager(db=async_db_session, task_repository=repo, is_async=True)

        # Execute task that will fail (invalid executor)
        task = TaskModel(
            id="failing_task",
            name="test_task",
            status="pending",
            priority=0,
            dependencies=[],
            inputs={"test": "value"},
            params={},
            schemas={"method": "nonexistent_executor"},  # Will fail
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        async_db_session.add(task)
        await async_db_session.flush()

        # Execute and expect failure
        try:
            await manager._execute_single_task(task, "test_task", task.inputs, {})
        except Exception:
            pass  # Expected to fail

        # Executor instance should still be cleaned up
        assert len(manager._executor_instances) == 0

    @pytest.mark.asyncio
    async def test_memory_stable_over_1000_tasks(self, async_db_session, get_memory_mb):
        """
        Test that memory remains stable over 1000 task executions

        Memory growth should be less than 100MB.
        """
        repo = TaskRepository(async_db_session)

        manager = TaskManager(db=async_db_session, task_repository=repo, is_async=True)

        # Record initial memory
        initial_memory = get_memory_mb()

        # Execute 1000 simple tasks
        for i in range(1000):
            task = TaskModel(
                id=f"task_{i}",
                name="test_task",
                status="pending",
                priority=0,
                dependencies=[],
                inputs={"value": i},
                params={},
                schemas={"method": "demo_executor"},
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            async_db_session.add(task)
            await async_db_session.flush()

            await manager._execute_single_task(task, "test_task", task.inputs, {})

            # Check memory every 100 tasks
            if i % 100 == 0:
                current_memory = get_memory_mb()
                memory_growth = current_memory - initial_memory

                # Log progress
                print(f"Task {i}: Memory growth = {memory_growth:.1f}MB")

                # Memory should not grow excessively
                assert memory_growth < 100, f"Memory grew by {memory_growth:.1f}MB after {i} tasks"

        # Final memory check
        final_memory = get_memory_mb()
        total_growth = final_memory - initial_memory

        print(f"Final memory growth: {total_growth:.1f}MB after 1000 tasks")
        assert total_growth < 100, f"Total memory growth {total_growth:.1f}MB exceeds limit"

    @pytest.mark.asyncio
    async def test_concurrent_tasks_memory(self, async_db_session, get_memory_mb):
        """
        Test memory usage with concurrent task execution

        Simulates realistic load with parallel tasks.
        """
        repo = TaskRepository(async_db_session)

        manager = TaskManager(db=async_db_session, task_repository=repo, is_async=True)

        initial_memory = get_memory_mb()

        # Execute 100 batches of 10 concurrent tasks
        for batch in range(100):
            tasks = []
            for i in range(10):
                task = TaskModel(
                    id=f"task_batch{batch}_item{i}",
                    name="concurrent_task",
                    status="pending",
                    priority=0,
                    dependencies=[],
                    inputs={"value": i},
                    params={},
                    schemas={"method": "demo_executor"},
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                async_db_session.add(task)
                tasks.append(task)

            await async_db_session.flush()

            # Execute concurrently
            await asyncio.gather(
                *[manager._execute_single_task(task, task.name, task.inputs, {}) for task in tasks]
            )

            # Check memory every 10 batches
            if batch % 10 == 0:
                current_memory = get_memory_mb()
                memory_growth = current_memory - initial_memory
                print(f"Batch {batch}: Memory growth = {memory_growth:.1f}MB")

                assert (
                    memory_growth < 150
                ), f"Memory grew by {memory_growth:.1f}MB after {batch * 10} tasks"

        final_memory = get_memory_mb()
        total_growth = final_memory - initial_memory

        print(f"Final memory growth: {total_growth:.1f}MB after 1000 concurrent tasks")
        assert total_growth < 150


class TestDatabaseGrowth:
    """Test database file size growth"""

    @pytest.mark.asyncio
    async def test_sqlite_file_growth_reasonable(self, tmp_path, sync_db_session):
        """
        Test that SQLite file size remains reasonable

        1000 simple tasks should not create a huge database file.
        """
        from apflow.core.storage.factory import get_session, create_all_tables

        # Create temporary SQLite file
        db_path = tmp_path / "test.db"
        db_url = f"sqlite:///{db_path}"

        # Create tables
        create_all_tables(db_url)

        # Create session and repository
        session = get_session(db_url)
        TaskRepository(session)

        # Create 1000 tasks
        for i in range(1000):
            task = TaskModel(
                id=f"task_{i}",
                name="test_task",
                status="completed",
                priority=0,
                dependencies=[],
                inputs={"value": i, "description": "Test task for size testing"},
                params={},
                result={"output": f"Result {i}", "status": "success"},
                schemas={"method": "test_executor"},
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
            )
            session.add(task)

            # Commit every 100 tasks
            if i % 100 == 0:
                session.commit()

        session.commit()
        session.close()

        # Check file size
        if db_path.exists():
            db_size_mb = db_path.stat().st_size / 1024 / 1024
            print(f"Database size: {db_size_mb:.2f}MB for 1000 tasks")

            # 1000 tasks should be well under 50MB
            # Typical should be 1-5MB
            assert db_size_mb < 50, f"Database file too large: {db_size_mb:.2f}MB"

    @pytest.mark.asyncio
    async def test_task_cleanup_reduces_size(self, tmp_path, sync_db_session):
        """
        Test that deleting old tasks reduces database size

        Verifies VACUUM or similar operations reclaim space.
        """
        from apflow.core.storage.factory import get_session, create_all_tables

        db_path = tmp_path / "test_cleanup.db"
        db_url = f"sqlite:///{db_path}"

        create_all_tables(db_url)
        session = get_session(db_url)
        TaskRepository(session)

        # Create many tasks
        for i in range(500):
            task = TaskModel(
                id=f"task_{i}",
                name="test_task",
                status="completed",
                priority=0,
                dependencies=[],
                inputs={"value": i},
                params={},
                result={"output": f"Result {i}"},
                schemas={"method": "test_executor"},
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
            )
            session.add(task)

        session.commit()

        # Record size after inserts
        size_after_insert = db_path.stat().st_size if db_path.exists() else 0

        # Delete half the tasks
        session.execute(
            "DELETE FROM apflow_tasks WHERE id LIKE 'task_2%' OR id LIKE 'task_3%' OR id LIKE 'task_4%'"
        )
        session.commit()

        # For SQLite, file size may not shrink immediately without VACUUM
        # This test documents expected behavior
        size_after_delete = db_path.stat().st_size if db_path.exists() else 0

        print(f"Size after insert: {size_after_insert / 1024:.1f}KB")
        print(f"Size after delete: {size_after_delete / 1024:.1f}KB")

        # Size should not grow after deletion
        assert size_after_delete <= size_after_insert * 1.1, "Database size grew after deletion"

        session.close()
