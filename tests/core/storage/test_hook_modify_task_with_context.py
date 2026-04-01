"""
Test hooks modifying task fields using hook context

This test file covers scenarios where hooks (pre_hook, post_hook, task_tree_hook)
modify task fields and use the hook context to access the database session.

Key questions answered:
1. Can hooks modify task.inputs and have it auto-persisted?
   Answer: YES - TaskManager detects changes to task.inputs and auto-saves them

2. Can hooks modify other task fields (status, name, priority, etc.)?
   Answer: YES - but requires explicit call to repository.update_task_* methods

3. Do hooks share the same session as TaskManager?
   Answer: YES - get_hook_repository() returns the same repository/session instance

4. Can hooks query other tasks using the shared session?
   Answer: YES - fully supported via get_hook_repository()
"""

import pytest
from apflow import register_pre_hook, register_post_hook, clear_config
from apflow.core.storage.context import get_hook_repository
from apflow.core.execution.task_manager import TaskManager
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.core.types import TaskTreeNode


@pytest.mark.asyncio
async def test_pre_hook_modifies_inputs_auto_persisted(sync_db_session):
    """Pre-hook modifies task.inputs - should be auto-persisted by TaskManager"""
    clear_config()
    hook_calls = []

    @register_pre_hook
    async def modify_inputs_hook(task):
        """Modify task.inputs directly - TaskManager will detect and save"""
        hook_calls.append({"task_id": task.id, "action": "modify_inputs"})
        if task.inputs is None:
            task.inputs = {}
        task.inputs["modified_by_hook"] = True
        task.inputs["hook_timestamp"] = "2024-01-01T00:00:00Z"

    try:
        repo = TaskRepository(sync_db_session)

        # Create task with initial inputs
        task = await repo.create_task(
            name="Test Task",
            user_id="test-user",
            inputs={"original": "value"},
        )
        task_id = task.id

        # Create task tree
        task_tree = TaskTreeNode(task)

        # Execute task tree - pre_hook should modify inputs
        manager = TaskManager(sync_db_session)
        await manager.distribute_task_tree(task_tree, use_callback=False)

        # Verify hook was called
        assert len(hook_calls) == 1
        assert hook_calls[0]["task_id"] == task_id

        # Verify inputs were persisted to database
        updated_task = await repo.get_task_by_id(task_id)
        assert updated_task.inputs is not None
        assert updated_task.inputs["modified_by_hook"] is True
        assert updated_task.inputs["hook_timestamp"] == "2024-01-01T00:00:00Z"
        assert updated_task.inputs["original"] == "value"
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_pre_hook_modifies_other_fields_requires_explicit_save(sync_db_session):
    """Pre-hook modifies task fields other than inputs - requires explicit repository call"""
    clear_config()
    hook_calls = []

    @register_pre_hook
    async def modify_task_fields_hook(task):
        """Modify task fields using hook context repository"""
        hook_calls.append({"task_id": task.id, "action": "modify_fields"})

        # Get repository from hook context
        repo = get_hook_repository()
        assert repo is not None, "Hook context should provide repository"

        # Modify task name using repository
        await repo.update_task(task.id, name="Modified by Hook")

        # Modify task priority using repository
        await repo.update_task(task.id, priority=10)

    try:
        repo = TaskRepository(sync_db_session)

        # Create task
        task = await repo.create_task(
            name="Original Name",
            user_id="test-user",
            priority=1,
        )
        task_id = task.id

        # Create task tree
        task_tree = TaskTreeNode(task)

        # Execute task tree
        manager = TaskManager(sync_db_session)
        await manager.distribute_task_tree(task_tree, use_callback=False)

        # Verify hook was called
        assert len(hook_calls) == 1

        # Verify fields were updated in database
        updated_task = await repo.get_task_by_id(task_id)
        assert updated_task.name == "Modified by Hook"
        assert updated_task.priority == 10
    finally:
        clear_config()


@pytest.mark.asyncio
async def test_post_hook_modifies_task_after_execution(sync_db_session):
    """Post-hook can modify task after execution using hook context"""
    clear_config()
    hook_calls = []

    @register_post_hook
    async def modify_after_execution_hook(task, inputs, result):
        """Post-hook modifies task using repository"""
        hook_calls.append(
            {
                "task_id": task.id,
                "task_status": task.status,
                "result": result,
            }
        )

        # Get repository from hook context
        repo = get_hook_repository()
        assert repo is not None

        # Add metadata to task params
        await repo.update_task(
            task.id,
            params={
                "post_hook_executed": True,
                "execution_result": result,
            },
        )

    try:
        repo = TaskRepository(sync_db_session)

        # Create task
        task = await repo.create_task(
            name="Test Task",
            user_id="test-user",
            params={"resource": "cpu", "executor_id": "aggregate_results_executor"},
        )
        task_id = task.id

        # Create task tree
        task_tree = TaskTreeNode(task)

        # Execute task tree
        manager = TaskManager(sync_db_session)
        await manager.distribute_task_tree(task_tree, use_callback=True)

        # Verify hook was called
        assert len(hook_calls) == 1

        # Verify params were updated
        updated_task = await repo.get_task_by_id(task_id)
        assert updated_task.params is not None
        assert updated_task.params.get("post_hook_executed") is True
        assert "execution_result" in updated_task.params
    finally:
        clear_config()
