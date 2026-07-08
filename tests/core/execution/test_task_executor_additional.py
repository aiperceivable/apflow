"""
Additional tests for TaskExecutor to increase coverage
"""

import threading

import pytest
from apflow.core.execution.task_executor import TaskExecutor
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.core.types import TaskTreeNode


class TestTaskExecutorAdditional:
    """Additional tests for TaskExecutor to achieve 80%+ coverage"""

    @pytest.mark.asyncio
    async def test_is_task_running_true(self):
        """Test is_task_running returns True for running task"""
        executor = TaskExecutor()
        task_id = "test-task-running"

        await executor.start_task_tracking(task_id)
        assert executor.is_task_running(task_id) is True

        await executor.stop_task_tracking(task_id)

    @pytest.mark.asyncio
    async def test_is_task_running_false(self):
        """Test is_task_running returns False for non-running task"""
        executor = TaskExecutor()

        assert executor.is_task_running("non-existent-task") is False

    @pytest.mark.asyncio
    async def test_start_task_tracking_duplicate(self):
        """Test start_task_tracking with duplicate task ID"""
        executor = TaskExecutor()
        task_id = "dup-task"

        await executor.start_task_tracking(task_id)
        await executor.start_task_tracking(task_id)  # Should not raise error

        assert executor.is_task_running(task_id) is True
        await executor.stop_task_tracking(task_id)

    @pytest.mark.asyncio
    async def test_stop_task_tracking_not_started(self):
        """Test stop_task_tracking for task that was never started"""
        executor = TaskExecutor()
        task_id = "never-started"

        # Should not raise error
        await executor.stop_task_tracking(task_id)

    @pytest.mark.asyncio
    async def test_refresh_config(self):
        """Test refresh_config method updates hooks"""
        executor = TaskExecutor()

        # Should not raise error
        executor.refresh_config()

        # Check hooks are populated
        assert executor.pre_hooks is not None
        assert executor.post_hooks is not None

    @pytest.mark.asyncio
    async def test_get_task_status(self):
        """Test get_task_status method"""
        executor = TaskExecutor()
        task_id = "status-test-task"

        # Start tracking
        await executor.start_task_tracking(task_id)

        # Get status
        status = executor.get_task_status(task_id)
        assert status is not None

        await executor.stop_task_tracking(task_id)

    @pytest.mark.asyncio
    async def test_get_all_running_tasks(self):
        """Test get_all_running_tasks method"""
        executor = TaskExecutor()
        task_id1 = "running-task-1"
        task_id2 = "running-task-2"

        await executor.start_task_tracking(task_id1)
        await executor.start_task_tracking(task_id2)

        running_tasks = executor.get_all_running_tasks()
        assert task_id1 in running_tasks
        assert task_id2 in running_tasks

        await executor.stop_task_tracking(task_id1)
        await executor.stop_task_tracking(task_id2)

    @pytest.mark.asyncio
    async def test_get_running_tasks_count(self):
        """Test get_running_tasks_count method"""
        executor = TaskExecutor()
        task_id = "count-test-task"

        initial_count = executor.get_running_tasks_count()

        await executor.start_task_tracking(task_id)
        new_count = executor.get_running_tasks_count()
        assert new_count == initial_count + 1

        await executor.stop_task_tracking(task_id)

    @pytest.mark.asyncio
    async def test_cancel_task_not_running(self, sync_db_session):
        """Test cancel_task when task is not running"""
        repo = TaskRepository(sync_db_session)
        task = await repo.create_task(
            name="cancel-test",
            user_id="test-user",
            schemas={"method": "rest_executor"},
        )

        executor = TaskExecutor()
        result = await executor.cancel_task(
            task_id=task.id, error_message="Test cancel", db_session=sync_db_session
        )

        # Should return cancelled status
        assert "status" in result

    @pytest.mark.asyncio
    async def test_execute_tasks_with_db_session(self, sync_db_session):
        """Test execute_tasks method with explicit db_session"""
        executor = TaskExecutor()

        tasks = [
            {
                "id": "exec-test-task",
                "name": "Execute Test",
                "user_id": "test-user",
                "schemas": {"method": "rest_executor"},
                "inputs": {"resource": "cpu"},
            }
        ]

        result = await executor.execute_tasks(
            tasks=tasks,
            db_session=sync_db_session,
            use_demo=True,  # Use demo mode to avoid real execution
        )

        assert "status" in result

    @pytest.mark.asyncio
    async def test_execute_tasks_empty_raises_error(self, sync_db_session):
        """Test execute_tasks with empty tasks array raises error"""
        executor = TaskExecutor()

        with pytest.raises(ValueError, match="No tasks provided"):
            await executor.execute_tasks(tasks=[], db_session=sync_db_session)

    @pytest.mark.asyncio
    async def test_execute_task_by_id_task_not_found(self, sync_db_session):
        """Test execute_task_by_id when task doesn't exist"""
        executor = TaskExecutor()

        with pytest.raises(ValueError, match="not found"):
            await executor.execute_task_by_id(
                task_id="nonexistent-task-id", db_session=sync_db_session
            )

    @pytest.mark.asyncio
    async def test_execute_task_by_id_success(self, sync_db_session):
        """Test execute_task_by_id with valid task"""
        repo = TaskRepository(sync_db_session)
        task = await repo.create_task(
            name="exec-by-id-test",
            user_id="test-user",
            schemas={"method": "rest_executor"},
            inputs={"resource": "cpu"},
        )

        executor = TaskExecutor()
        result = await executor.execute_task_by_id(
            task_id=task.id, db_session=sync_db_session, use_demo=True
        )

        assert "status" in result

    @pytest.mark.asyncio
    async def test_build_task_tree_from_tasks_missing_user_id(self):
        """Test _build_task_tree_from_tasks with missing user_id"""
        executor = TaskExecutor()

        tasks = [
            {
                "id": "task-no-user",
                "name": "No User Task",
                # Missing user_id
            }
        ]

        with pytest.raises(ValueError, match="missing required user_id"):
            executor._build_task_tree_from_tasks(tasks)

    @pytest.mark.asyncio
    async def test_build_task_tree_from_tasks_no_root(self):
        """Test _build_task_tree_from_tasks with no root task"""
        executor = TaskExecutor()

        tasks = [
            {
                "id": "child-task",
                "name": "Child Task",
                "user_id": "test-user",
                "parent_id": "some-parent",  # Has parent, not root
            }
        ]

        with pytest.raises(ValueError, match="No root task found"):
            executor._build_task_tree_from_tasks(tasks)

    @pytest.mark.asyncio
    async def test_build_task_tree_from_tasks_success(self):
        """Test _build_task_tree_from_tasks with valid tasks"""
        executor = TaskExecutor()

        tasks = [
            {"id": "root-task", "name": "Root Task", "user_id": "test-user"},
            {
                "id": "child-task",
                "name": "Child Task",
                "user_id": "test-user",
                "parent_id": "root-task",
            },
        ]

        tree = executor._build_task_tree_from_tasks(tasks)

        assert tree.task.id == "root-task"
        assert len(tree.children) == 1
        assert tree.children[0].task.id == "child-task"

    @pytest.mark.asyncio
    async def test_mark_tasks_for_reexecution(self, sync_db_session):
        """Test _mark_tasks_for_reexecution method"""
        repo = TaskRepository(sync_db_session)

        # Create a task and set it to failed status
        task = await repo.create_task(name="failed-task", user_id="test-user")
        # Update to failed status
        await repo.update_task(task.id, status="failed")
        task = await repo.get_task_by_id(task.id)

        task_tree = TaskTreeNode(task)
        executor = TaskExecutor()

        marked_ids = executor._mark_tasks_for_reexecution(task_tree)

        # Failed tasks should be marked for re-execution
        assert task.id in marked_ids


class TestTaskExecutorSingletonThreadSafety:
    """Regression: TaskExecutor's __new__/__init__ were unsynchronized, unlike
    the sibling TaskTracker singleton in the same module. A race could
    produce two instances, with one never receiving set_distributed_runtime()
    and bypassing the leader-only execution guard. (Review CRITICAL #28)
    """

    def test_singleton_returns_same_instance(self):
        executor1 = TaskExecutor()
        executor2 = TaskExecutor()
        assert executor1 is executor2

    def test_singleton_thread_safe(self):
        """Concurrent construction from many threads must all return the
        exact same instance — mirrors TaskTracker's own thread-safety test."""
        instances: list[TaskExecutor] = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)

        def create_instance() -> None:
            barrier.wait()
            instance = TaskExecutor()
            with lock:
                instances.append(instance)

        threads = [threading.Thread(target=create_instance) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(instances) == 10
        for instance in instances:
            assert instance is instances[0]

    def test_distributed_runtime_set_on_the_single_shared_instance(self):
        """A distributed_runtime set on one reference must be visible via
        every other reference obtained concurrently — proving there is only
        ever one underlying instance to wire up."""
        instances: list[TaskExecutor] = []
        barrier = threading.Barrier(10)

        def create_instance() -> None:
            barrier.wait()
            instances.append(TaskExecutor())

        threads = [threading.Thread(target=create_instance) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        sentinel_runtime = object()
        instances[0].set_distributed_runtime(sentinel_runtime)

        for instance in instances:
            assert instance.distributed_runtime is sentinel_runtime


class TestTaskExecutorRequireExisting:
    """Test TaskExecutor with require_existing_tasks mode"""

    @pytest.mark.asyncio
    async def test_execute_tasks_require_existing_task_not_found(self, sync_db_session):
        """Test execute_tasks with require_existing_tasks when task doesn't exist"""
        executor = TaskExecutor()

        tasks = [{"id": "nonexistent-task-id"}]

        with pytest.raises(ValueError, match="not found"):
            await executor.execute_tasks(
                tasks=tasks, require_existing_tasks=True, db_session=sync_db_session
            )

    @pytest.mark.asyncio
    async def test_execute_tasks_require_existing_success(self, sync_db_session):
        """Test execute_tasks with require_existing_tasks=True for existing task"""
        repo = TaskRepository(sync_db_session)
        task = await repo.create_task(
            name="existing-task",
            user_id="test-user",
            schemas={"method": "rest_executor"},
            inputs={"resource": "cpu"},
        )

        executor = TaskExecutor()

        tasks = [{"id": task.id}]

        result = await executor.execute_tasks(
            tasks=tasks, require_existing_tasks=True, db_session=sync_db_session, use_demo=True
        )

        assert "status" in result

    @pytest.mark.asyncio
    async def test_execute_tasks_with_parent_child(self, sync_db_session):
        """Test execute_tasks with parent-child relationship"""
        executor = TaskExecutor()

        tasks = [
            {
                "id": "parent-task",
                "name": "Parent Task",
                "user_id": "test-user",
                "schemas": {"method": "rest_executor"},
                "inputs": {"resource": "cpu"},
            },
            {
                "id": "child-task",
                "name": "Child Task",
                "user_id": "test-user",
                "parent_id": "parent-task",
                "schemas": {"method": "rest_executor"},
                "inputs": {"resource": "memory"},
            },
        ]

        result = await executor.execute_tasks(
            tasks=tasks, db_session=sync_db_session, use_demo=True
        )

        assert "status" in result

    @pytest.mark.asyncio
    async def test_execute_tasks_with_dependencies(self, sync_db_session):
        """Test execute_tasks with task dependencies"""
        executor = TaskExecutor()

        # Both tasks need to be in same tree, dep-task is child of main-task
        tasks = [
            {
                "id": "main-task",
                "name": "Main Task",
                "user_id": "test-user",
                "schemas": {"method": "rest_executor"},
                "inputs": {"resource": "memory"},
            },
            {
                "id": "dep-task",
                "name": "Dependency Task",
                "user_id": "test-user",
                "parent_id": "main-task",
                "schemas": {"method": "rest_executor"},
                "inputs": {"resource": "cpu"},
                "dependencies": [{"id": "main-task", "required": True}],
            },
        ]

        result = await executor.execute_tasks(
            tasks=tasks, db_session=sync_db_session, use_demo=True
        )

        assert "status" in result

    @pytest.mark.asyncio
    async def test_load_existing_task_tree_invalid_format(self, sync_db_session):
        """Test _load_existing_task_tree with invalid task format"""
        executor = TaskExecutor()

        # Task dict without id field
        tasks = [{"name": "no-id-task"}]

        with pytest.raises(ValueError, match="must have 'id' field"):
            await executor._load_existing_task_tree(tasks, sync_db_session)

    @pytest.mark.asyncio
    async def test_load_existing_task_tree_string_ids(self, sync_db_session):
        """Test _load_existing_task_tree with string task IDs"""
        repo = TaskRepository(sync_db_session)
        task = await repo.create_task(name="string-id-task", user_id="test-user")

        executor = TaskExecutor()

        # Pass task IDs as strings
        tree = await executor._load_existing_task_tree([task.id], sync_db_session)

        assert tree.task.id == task.id

    @pytest.mark.asyncio
    async def test_collect_all_dependencies_empty(self, sync_db_session):
        """Test _collect_all_dependencies with no dependencies"""
        repo = TaskRepository(sync_db_session)
        task = await repo.create_task(name="no-deps-task", user_id="test-user")

        executor = TaskExecutor()

        collected = await executor._collect_all_dependencies(
            task_id=task.id,
            task_repository=repo,
            collected=set(),
            processed=set(),
            all_tasks_in_tree=[task],
        )

        # Should return empty set since task has no dependencies
        assert len(collected) == 0

    @pytest.mark.asyncio
    async def test_collect_all_dependencies_with_deps(self, sync_db_session):
        """Test _collect_all_dependencies with dependencies"""
        repo = TaskRepository(sync_db_session)

        dep_task = await repo.create_task(name="dep-task", user_id="test-user")

        main_task = await repo.create_task(
            name="main-task",
            user_id="test-user",
            dependencies=[{"id": dep_task.id, "required": True}],
        )

        executor = TaskExecutor()

        collected = await executor._collect_all_dependencies(
            task_id=main_task.id,
            task_repository=repo,
            collected=set(),
            processed=set(),
            all_tasks_in_tree=[dep_task, main_task],
        )

        assert dep_task.id in collected
