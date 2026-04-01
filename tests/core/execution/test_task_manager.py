"""
Test TaskManager functionality
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from apflow.core.execution.task_manager import TaskManager
from apflow.core.types import TaskTreeNode
from apflow.core.storage.sqlalchemy.models import TaskModel


class TestTaskManager:
    """Test TaskManager core functionality"""

    @pytest.mark.asyncio
    async def test_task_manager_initialization_sync(self, sync_db_session):
        """Test TaskManager initialization with sync session"""
        # Explicitly pass empty hooks to avoid dependency on global config state
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
        assert task_manager.db == sync_db_session
        assert task_manager.is_async is False
        assert task_manager.root_task_id is None
        assert task_manager.stream is False
        assert task_manager.streaming_final is False
        assert task_manager.pre_hooks == []
        assert task_manager.post_hooks == []

    @pytest.mark.asyncio
    async def test_task_manager_initialization_async(self, async_db_session):
        """Test TaskManager initialization with async session"""
        # Explicitly pass empty hooks to avoid dependency on global config state
        task_manager = TaskManager(async_db_session, pre_hooks=[], post_hooks=[])
        assert task_manager.db == async_db_session
        assert task_manager.is_async is True
        assert task_manager.pre_hooks == []
        assert task_manager.post_hooks == []

    @pytest.mark.asyncio
    async def test_task_manager_with_hooks(self, sync_db_session):
        """Test TaskManager initialization with pre and post hooks"""
        pre_hook_called = []
        post_hook_called = []

        async def pre_hook(task):
            pre_hook_called.append((task.id, task.inputs))
            # Modify task.inputs to demonstrate hook can transform data
            if task.inputs and "url" in task.inputs:
                task.inputs["url"] = task.inputs["url"].strip()

        async def post_hook(task, inputs, result):
            post_hook_called.append((task.id, inputs, result))

        task_manager = TaskManager(sync_db_session, pre_hooks=[pre_hook], post_hooks=[post_hook])

        assert len(task_manager.pre_hooks) == 1
        assert len(task_manager.post_hooks) == 1

        # Create and execute a task to test hooks
        # Use rest_executor which doesn't require additional params
        task = await task_manager.task_repository.create_task(
            name="Test Task",
            user_id="test-user",
            inputs={"resource": "cpu"},
            schemas={"method": "rest_executor"},
        )

        # Create a simple task tree
        task_tree = TaskTreeNode(task)

        # Execute task tree (this will trigger hooks)
        await task_manager.distribute_task_tree(task_tree, use_callback=False)

        # Verify pre-hook was called
        assert len(pre_hook_called) == 1
        assert pre_hook_called[0][0] == task.id
        # Verify inputs was modified by pre-hook
        # Note: The actual inputs modification happens in the hook
        # Note: rest_executor doesn't modify inputs, so we just verify hook was called

        # Verify post-hook was called
        assert len(post_hook_called) == 1
        assert post_hook_called[0][0] == task.id

    @pytest.mark.asyncio
    async def test_task_manager_with_sync_hooks(self, sync_db_session):
        """Test TaskManager with synchronous hooks"""
        pre_hook_called = []
        post_hook_called = []

        def sync_pre_hook(task):
            pre_hook_called.append((task.id, task.inputs))

        def sync_post_hook(task, inputs, result):
            post_hook_called.append((task.id, inputs, result))

        task_manager = TaskManager(
            sync_db_session, pre_hooks=[sync_pre_hook], post_hooks=[sync_post_hook]
        )

        # Use rest_executor which doesn't require additional params
        task = await task_manager.task_repository.create_task(
            name="Test Task",
            user_id="test-user",
            inputs={"resource": "cpu"},
            schemas={"method": "rest_executor"},
        )

        task_tree = TaskTreeNode(task)
        await task_manager.distribute_task_tree(task_tree, use_callback=False)

        # Verify hooks were called
        assert len(pre_hook_called) == 1
        assert len(post_hook_called) == 1

    @pytest.mark.asyncio
    async def test_task_manager_hooks_error_handling(self, sync_db_session):
        """Test that hook errors don't fail task execution"""
        pre_hook_called = []
        post_hook_called = []

        async def failing_pre_hook(task):
            pre_hook_called.append((task.id, task.inputs))
            raise ValueError("Pre-hook error")

        async def failing_post_hook(task, inputs, result):
            post_hook_called.append((task.id, inputs, result))
            raise ValueError("Post-hook error")

        task_manager = TaskManager(
            sync_db_session, pre_hooks=[failing_pre_hook], post_hooks=[failing_post_hook]
        )

        # Use rest_executor which doesn't require additional params
        task = await task_manager.task_repository.create_task(
            name="Test Task",
            user_id="test-user",
            inputs={"resource": "cpu"},
            schemas={"method": "rest_executor"},
        )

        task_tree = TaskTreeNode(task)

        # Task execution should succeed despite hook errors
        await task_manager.distribute_task_tree(task_tree, use_callback=False)

        # Verify hooks were called
        assert len(pre_hook_called) == 1
        assert len(post_hook_called) == 1

        # Verify task completed successfully
        updated_task = await task_manager.task_repository.get_task_by_id(task.id)
        assert updated_task.status == "completed"

    @pytest.mark.asyncio
    async def test_create_task(self, sync_db_session):
        """Test task creation using task_repository"""
        task_manager = TaskManager(sync_db_session)

        task = await task_manager.task_repository.create_task(
            name="Test Task",
            user_id="test-user",
            params={"test": "value"},
            schemas={"method": "crewai_executor", "model": "openai/gpt-4o"},
        )

        assert task.name == "Test Task"
        assert task.user_id == "test-user"
        assert task.params == {"test": "value"}
        assert task.status == "pending"
        assert task.progress == 0.0
        assert task.schemas["method"] == "crewai_executor"
        assert task.schemas["model"] == "openai/gpt-4o"

        # Verify persistence (already committed by create_task)
        retrieved = sync_db_session.query(TaskModel).filter(TaskModel.id == task.id).first()
        assert retrieved is not None
        assert retrieved.name == "Test Task"

    @pytest.mark.asyncio
    async def test_task_tree_node_calculate_progress(self, sync_db_session):
        """Test TaskTreeNode progress calculation"""
        # Create parent task
        parent_task = TaskModel(
            id="parent-1",
            user_id="test-user",
            name="Parent Task",
            status="pending",
            progress=0.0,
            has_children=True,
        )
        sync_db_session.add(parent_task)

        # Create child tasks
        child1 = TaskModel(
            id="child-1",
            user_id="test-user",
            parent_id="parent-1",
            name="Child 1",
            status="completed",
            progress=1.0,
        )
        child2 = TaskModel(
            id="child-2",
            user_id="test-user",
            parent_id="parent-1",
            name="Child 2",
            status="completed",
            progress=1.0,
        )
        sync_db_session.add_all([child1, child2])
        sync_db_session.commit()

        # Build tree
        parent_node = TaskTreeNode(task=parent_task)
        child1_node = TaskTreeNode(task=child1)
        child2_node = TaskTreeNode(task=child2)
        parent_node.add_child(child1_node)
        parent_node.add_child(child2_node)

        # Calculate progress
        progress = parent_node.calculate_progress()
        assert progress == 1.0  # Average of 1.0 and 1.0

    @pytest.mark.asyncio
    async def test_task_tree_node_calculate_status(self, sync_db_session):
        """Test TaskTreeNode status calculation"""
        # Create parent task
        parent_task = TaskModel(
            id="parent-1",
            user_id="test-user",
            name="Parent Task",
            status="pending",
            has_children=True,
        )

        # Create child tasks with different statuses
        child1 = TaskModel(
            id="child-1",
            user_id="test-user",
            parent_id="parent-1",
            name="Child 1",
            status="completed",
        )
        child2 = TaskModel(
            id="child-2", user_id="test-user", parent_id="parent-1", name="Child 2", status="failed"
        )

        # Build tree
        parent_node = TaskTreeNode(task=parent_task)
        child1_node = TaskTreeNode(task=child1)
        child2_node = TaskTreeNode(task=child2)
        parent_node.add_child(child1_node)
        parent_node.add_child(child2_node)

        # Calculate status - should be "failed" (highest priority)
        status = parent_node.calculate_status()
        assert status == "failed"

    @pytest.mark.asyncio
    async def test_are_dependencies_satisfied_no_dependencies(self, sync_db_session):
        """Test dependency checking with no dependencies"""
        task_manager = TaskManager(sync_db_session)

        task = TaskModel(
            id="task-1", user_id="test-user", name="Task 1", status="pending", dependencies=[]
        )

        result = await task_manager._are_dependencies_satisfied(task)
        assert result is True

    @pytest.mark.asyncio
    async def test_are_dependencies_satisfied_with_satisfied_dependencies(self, sync_db_session):
        """Test dependency checking with satisfied dependencies"""
        task_manager = TaskManager(sync_db_session)

        # Create a root task to ensure both tasks are in the same tree
        root_task = TaskModel(
            id="root-task-1", user_id="test-user", name="Root Task", status="pending"
        )
        sync_db_session.add(root_task)
        sync_db_session.commit()

        # Create dependency task as child of root
        dep_task = TaskModel(
            id="dep-task-1",
            user_id="test-user",
            name="Dependency Task",
            status="completed",
            result={"output": "data"},
            parent_id=root_task.id,  # Same tree
        )
        sync_db_session.add(dep_task)
        sync_db_session.commit()

        # Create task with dependency, also as child of root
        task = TaskModel(
            id="task-1",
            user_id="test-user",
            name="Task 1",
            status="pending",
            dependencies=[{"id": "dep-task-1", "required": True}],
            parent_id=root_task.id,  # Same tree
        )
        sync_db_session.add(task)
        sync_db_session.commit()

        result = await task_manager._are_dependencies_satisfied(task)
        assert result is True

    @pytest.mark.asyncio
    async def test_are_dependencies_satisfied_with_unsatisfied_dependencies(self, sync_db_session):
        """Test dependency checking with unsatisfied dependencies"""
        task_manager = TaskManager(sync_db_session)

        # Create task with dependency that doesn't exist
        task = TaskModel(
            id="task-1",
            user_id="test-user",
            name="Task 1",
            status="pending",
            dependencies=[{"id": "non-existent-task", "required": True}],
        )
        sync_db_session.add(task)
        sync_db_session.commit()

        result = await task_manager._are_dependencies_satisfied(task)
        assert result is False

    @pytest.mark.asyncio
    async def test_resolve_task_dependencies(self, sync_db_session):
        """Test dependency resolution"""
        task_manager = TaskManager(sync_db_session)

        # Create a root task to ensure both tasks are in the same tree
        root_task = TaskModel(
            id="root-task-1", user_id="test-user", name="Root Task", status="pending"
        )
        sync_db_session.add(root_task)
        sync_db_session.commit()

        # Create dependency task with result, as child of root
        dep_task = TaskModel(
            id="dep-task-1",
            user_id="test-user",
            name="Dependency Task",
            status="completed",
            result={"url": "https://resolved.com", "data": "resolved"},
            parent_id=root_task.id,  # Same tree
        )
        sync_db_session.add(dep_task)
        sync_db_session.commit()

        # Create task with dependency and input_schema, also as child of root
        task = TaskModel(
            id="task-1",
            user_id="test-user",
            name="Task 1",
            status="pending",
            dependencies=[{"id": "dep-task-1", "required": True}],
            inputs={"existing": "value"},
            schemas={
                "input_schema": {
                    "properties": {"url": {"type": "string"}, "data": {"type": "string"}}
                }
            },
            parent_id=root_task.id,  # Same tree
        )
        sync_db_session.add(task)
        sync_db_session.commit()

        resolved_data = await task_manager.resolve_task_dependencies(task)

        # Should have resolved fields from dependency
        assert resolved_data["url"] == "https://resolved.com"
        assert resolved_data["data"] == "resolved"
        assert resolved_data["existing"] == "value"  # Existing data preserved

    @pytest.mark.asyncio
    async def test_distribute_task_tree_simple(self, sync_db_session):
        """Test simple task tree distribution"""
        task_manager = TaskManager(sync_db_session)

        # Create a simple task
        task = TaskModel(
            id="task-1",
            user_id="test-user",
            name="Simple Task",
            status="pending",
            schemas={"method": "crewai_executor"},
        )
        sync_db_session.add(task)
        sync_db_session.commit()

        # Build tree
        task_node = TaskTreeNode(task=task)

        # Mock agent execution
        with patch.object(
            task_manager, "_execute_single_task", new_callable=AsyncMock
        ) as mock_execute:
            mock_execute.return_value = None

            await task_manager.distribute_task_tree(task_node, use_callback=False)

            # Should have attempted to execute
            assert mock_execute.called

    @pytest.mark.asyncio
    async def test_distribute_task_tree_with_children(self, sync_db_session):
        """Test task tree distribution with children"""
        task_manager = TaskManager(sync_db_session)

        # Create parent task
        parent_task = TaskModel(
            id="parent-1",
            user_id="test-user",
            name="Parent Task",
            status="pending",
            has_children=True,
            priority=3,
        )

        # Create child tasks
        child1 = TaskModel(
            id="child-1",
            user_id="test-user",
            parent_id="parent-1",
            name="Child 1",
            status="pending",
            priority=1,
        )
        child2 = TaskModel(
            id="child-2",
            user_id="test-user",
            parent_id="parent-1",
            name="Child 2",
            status="pending",
            priority=1,
            dependencies=[{"id": "child-1", "required": True}],
        )

        sync_db_session.add_all([parent_task, child1, child2])
        sync_db_session.commit()

        # Build tree
        parent_node = TaskTreeNode(task=parent_task)
        child1_node = TaskTreeNode(task=child1)
        child2_node = TaskTreeNode(task=child2)
        parent_node.add_child(child1_node)
        parent_node.add_child(child2_node)

        # Mock execution
        with patch.object(
            task_manager, "_execute_single_task", new_callable=AsyncMock
        ) as mock_execute:
            # Mock child1 completion
            async def mock_execute_side_effect(task, use_callback):
                if task.id == "child-1":
                    task.status = "completed"
                    task.result = {"output": "child1 data"}
                    sync_db_session.commit()

            mock_execute.side_effect = mock_execute_side_effect

            await task_manager.distribute_task_tree(parent_node, use_callback=False)

            # Should have executed child tasks
            assert mock_execute.call_count >= 1


class TestTaskManagerExecutorLock:
    """Test _executor_lock protects _executor_instances from race conditions"""

    @pytest.mark.asyncio
    async def test_concurrent_cancel_and_execute(self, sync_db_session):
        """Test that concurrent cancel and execute don't race on _executor_instances"""
        task_manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])

        # Simulate concurrent writes and reads
        errors: list[Exception] = []

        async def write_executor(task_id: str) -> None:
            try:
                async with task_manager._executor_lock:
                    task_manager._executor_instances[task_id] = f"executor-{task_id}"
                await asyncio.sleep(0)  # Yield to event loop
                async with task_manager._executor_lock:
                    task_manager._executor_instances.pop(task_id, None)
            except Exception as e:
                errors.append(e)

        async def read_executor(task_id: str) -> None:
            try:
                async with task_manager._executor_lock:
                    task_manager._executor_instances.get(task_id)
            except Exception as e:
                errors.append(e)

        # Run many concurrent operations
        tasks = []
        for i in range(50):
            task_id = f"task-{i}"
            tasks.append(write_executor(task_id))
            tasks.append(read_executor(task_id))

        await asyncio.gather(*tasks)

        assert errors == [], f"Race condition detected: {errors}"
        assert task_manager._executor_instances == {}
