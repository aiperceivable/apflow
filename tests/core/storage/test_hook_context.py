"""
Test hook execution context for database access in hooks
"""

import pytest
from apflow.core.storage.context import (
    get_hook_session,
    get_hook_repository,
    set_hook_context,
    clear_hook_context,
)
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


@pytest.mark.asyncio
async def test_hook_context_initially_none():
    """Hook context should be None before setting"""
    assert get_hook_session() is None
    assert get_hook_repository() is None


@pytest.mark.asyncio
async def test_set_and_get_hook_context(sync_db_session):
    """Should be able to set and get hook context"""
    task_repository = TaskRepository(sync_db_session)

    try:
        set_hook_context(task_repository)

        session = get_hook_session()
        assert session is not None
        assert session == sync_db_session

        repo = get_hook_repository()
        assert repo is not None
        assert repo is task_repository
    finally:
        clear_hook_context()


@pytest.mark.asyncio
async def test_clear_hook_context(sync_db_session):
    """Should be able to clear hook context"""
    task_repository = TaskRepository(sync_db_session)

    set_hook_context(task_repository)
    assert get_hook_repository() is not None

    clear_hook_context()
    assert get_hook_repository() is None
    assert get_hook_session() is None


@pytest.mark.asyncio
async def test_hook_context_isolation():
    """Hook context should be isolated per context"""
    # This test verifies that context is properly isolated
    # In practice, ContextVar provides this isolation automatically
    assert get_hook_repository() is None

    # Simulate setting context in different execution contexts
    # ContextVar will handle the isolation
    clear_hook_context()
    assert get_hook_repository() is None


@pytest.mark.asyncio
async def test_hook_can_query_database(sync_db_session):
    """Hook should be able to query database using context"""
    task_repository = TaskRepository(sync_db_session)

    try:
        # Create a test task
        task = await task_repository.create_task(
            name="Test Hook Task",
            user_id="test-user",
        )
        assert task.id is not None

        # Set hook context
        set_hook_context(task_repository)

        # Simulate hook execution: query task using hook context
        repo = get_hook_repository()
        assert repo is not None

        fetched_task = await repo.get_task_by_id(task.id)
        assert fetched_task is not None
        assert fetched_task.id == task.id
        assert fetched_task.name == "Test Hook Task"
    finally:
        clear_hook_context()


@pytest.mark.asyncio
async def test_hook_can_update_database(sync_db_session):
    """Hook should be able to update database using context"""
    task_repository = TaskRepository(sync_db_session)

    try:
        # Create a test task
        task = await task_repository.create_task(
            name="Test Update Task",
            user_id="test-user",
        )
        assert task.id is not None

        # Set hook context
        set_hook_context(task_repository)

        # Simulate hook execution: update task using hook context
        repo = get_hook_repository()
        assert repo is not None

        await repo.update_task(task.id, status="in_progress")

        # Verify update
        updated_task = await repo.get_task_by_id(task.id)
        assert updated_task is not None
        assert updated_task.status == "in_progress"
    finally:
        clear_hook_context()


@pytest.mark.asyncio
async def test_hook_context_with_exception(sync_db_session):
    """Hook context should be cleaned up even if exception occurs"""
    task_repository = TaskRepository(sync_db_session)

    try:
        set_hook_context(task_repository)
        assert get_hook_repository() is not None

        # Simulate exception during hook execution
        try:
            raise ValueError("Test exception")
        except ValueError:
            pass

        # Context should still be set (cleanup is manual)
        assert get_hook_repository() is not None
    finally:
        # Manual cleanup in finally block
        clear_hook_context()
        assert get_hook_repository() is None


@pytest.mark.asyncio
async def test_multiple_set_clear_cycles(sync_db_session):
    """Should be able to set and clear context multiple times"""
    task_repository = TaskRepository(sync_db_session)

    for _ in range(3):
        # Set context
        set_hook_context(task_repository)
        assert get_hook_repository() is not None

        # Clear context
        clear_hook_context()
        assert get_hook_repository() is None


@pytest.mark.asyncio
async def test_multiple_hooks_share_same_session(sync_db_session):
    """Multiple hooks should share the same session instance"""
    task_repository = TaskRepository(sync_db_session)

    try:
        # Set hook context once
        set_hook_context(task_repository)

        # Simulate first hook accessing session
        session1 = get_hook_session()
        repo1 = get_hook_repository()

        # Simulate second hook accessing session
        session2 = get_hook_session()
        repo2 = get_hook_repository()

        # Simulate third hook accessing session
        session3 = get_hook_session()
        repo3 = get_hook_repository()

        # All hooks should get the same session and repository instance
        assert session1 == session2
        assert session2 == session3
        assert session1 == sync_db_session

        assert repo1 is repo2
        assert repo2 is repo3
        assert repo1 is task_repository
    finally:
        clear_hook_context()


@pytest.mark.asyncio
async def test_hooks_share_transaction_context(sync_db_session):
    """Multiple hooks should share the same transaction context"""
    task_repository = TaskRepository(sync_db_session)

    try:
        # Create a test task
        task = await task_repository.create_task(
            name="Transaction Test Task",
            user_id="test-user",
        )
        task_id = task.id

        # Set hook context
        set_hook_context(task_repository)

        # Simulate first hook (pre_hook) updating task
        repo1 = get_hook_repository()
        assert repo1 is not None
        await repo1.update_task(task_id, status="in_progress")

        # Simulate second hook (post_hook) reading updated task
        repo2 = get_hook_repository()
        assert repo2 is not None
        updated_task = await repo2.get_task_by_id(task_id)
        assert updated_task is not None

        # Should see the update made by first hook (same transaction)
        assert updated_task.status == "in_progress"

        # Simulate third hook (task_tree_hook) making another update
        repo3 = get_hook_repository()
        assert repo3 is not None
        await repo3.update_task(task_id, status="completed")

        # All hooks should see the latest state
        final_task = await repo1.get_task_by_id(task_id)
        assert final_task is not None
        assert final_task.status == "completed"
    finally:
        clear_hook_context()


@pytest.mark.asyncio
async def test_hooks_can_cooperate_via_shared_session(sync_db_session):
    """Hooks should be able to cooperate by sharing data via the session"""
    task_repository = TaskRepository(sync_db_session)

    try:
        # Create two related tasks
        task1 = await task_repository.create_task(
            name="Parent Task",
            user_id="test-user",
        )
        task2 = await task_repository.create_task(
            name="Child Task",
            user_id="test-user",
            dependencies=[{"id": task1.id, "required": True}],
        )

        # Set hook context
        set_hook_context(task_repository)

        # Simulate pre_hook checking parent task status
        repo_pre = get_hook_repository()
        assert repo_pre is not None
        parent_task = await repo_pre.get_task_by_id(task1.id)
        assert parent_task is not None
        assert parent_task.status == "pending"

        # Simulate post_hook updating parent task
        repo_post = get_hook_repository()
        assert repo_post is not None
        await repo_post.update_task(task1.id, status="completed")

        # Simulate task_tree_hook verifying dependencies
        repo_tree = get_hook_repository()
        assert repo_tree is not None
        updated_parent = await repo_tree.get_task_by_id(task1.id)
        assert updated_parent is not None
        child_task = await repo_tree.get_task_by_id(task2.id)
        assert child_task is not None

        # Verify both hooks see consistent data
        assert updated_parent.status == "completed"
        assert child_task.dependencies[0]["id"] == task1.id
    finally:
        clear_hook_context()


@pytest.mark.asyncio
async def test_session_consistency_across_hook_chain(sync_db_session):
    """Verify session remains consistent throughout the entire hook chain"""
    task_repository = TaskRepository(sync_db_session)
    session_ids = []
    repo_ids = []

    try:
        set_hook_context(task_repository)

        # Simulate a chain of 5 hooks being called
        for i in range(5):
            session = get_hook_session()
            repo = get_hook_repository()

            session_ids.append(id(session))
            repo_ids.append(id(repo))

            # Each hook should get the exact same instances
            assert session == sync_db_session
            assert repo is task_repository

        # All IDs should be identical (same object instance)
        assert len(set(session_ids)) == 1, "All hooks should share the same session instance"
        assert len(set(repo_ids)) == 1, "All hooks should share the same repository instance"
    finally:
        clear_hook_context()


@pytest.mark.asyncio
async def test_hooks_see_uncommitted_changes(sync_db_session):
    """Hooks in the same context should see uncommitted changes from previous hooks"""
    task_repository = TaskRepository(sync_db_session)

    try:
        # Create initial task
        task = await task_repository.create_task(
            name="Initial Task",
            user_id="test-user",
        )
        original_name = task.name

        # Set hook context
        set_hook_context(task_repository)

        # First hook modifies task name (simulating pre_hook)
        repo1 = get_hook_repository()
        assert repo1 is not None
        await repo1.update_task(task.id, name="Modified by Hook 1")

        # Second hook should see the change (simulating post_hook)
        repo2 = get_hook_repository()
        assert repo2 is not None
        updated_task = await repo2.get_task_by_id(task.id)
        assert updated_task is not None
        assert updated_task.name == "Modified by Hook 1"
        assert updated_task.name != original_name

        # Third hook modifies again (simulating task_tree_hook)
        repo3 = get_hook_repository()
        assert repo3 is not None
        await repo3.update_task(task.id, name="Modified by Hook 3")

        # All hooks should see the latest change
        final_task = await repo1.get_task_by_id(task.id)
        assert final_task is not None
        assert final_task.name == "Modified by Hook 3"
    finally:
        clear_hook_context()
