"""
Test TaskRepository functionality

This replaces test_task_model.py as it's more appropriate to test the repository
layer that provides actual functionality, rather than just the model layer.
"""

import uuid

import pytest
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.core.types import TaskTreeNode


class TestTaskRepository:
    """Test TaskRepository core functionality"""

    @pytest.mark.asyncio
    async def test_create_task_basic(self, sync_db_session):
        """Test creating a basic task"""
        repo = TaskRepository(sync_db_session)

        task = await repo.create_task(name="Test Task", user_id="test-user")

        assert task is not None
        assert task.name == "Test Task"
        assert task.user_id == "test-user"
        assert task.status == "pending"
        assert task.priority == 1
        assert task.progress == 0.0
        assert task.id is not None

    @pytest.mark.asyncio
    async def test_create_task_with_dependencies(self, sync_db_session):
        """Test creating a task with dependencies"""
        repo = TaskRepository(sync_db_session)

        # Create a task that will be a dependency
        dep_task = await repo.create_task(name="Dependency Task", user_id="test-user")

        # Create task with dependency
        task = await repo.create_task(
            name="Test Task",
            user_id="test-user",
            dependencies=[{"id": dep_task.id, "required": True}],
        )

        assert task.dependencies is not None
        assert len(task.dependencies) == 1
        assert task.dependencies[0]["id"] == dep_task.id
        assert task.dependencies[0]["required"] is True

    @pytest.mark.asyncio
    async def test_get_task_by_id(self, sync_db_session):
        """Test retrieving a task by ID"""
        repo = TaskRepository(sync_db_session)

        # Create a task
        created_task = await repo.create_task(name="Test Task", user_id="test-user")

        # Retrieve it
        retrieved_task = await repo.get_task_by_id(created_task.id)

        assert retrieved_task is not None
        assert retrieved_task.id == created_task.id
        assert retrieved_task.name == "Test Task"
        assert retrieved_task.user_id == "test-user"

    @pytest.mark.asyncio
    async def test_get_task_by_id_not_found(self, sync_db_session):
        """Test retrieving a non-existent task"""
        repo = TaskRepository(sync_db_session)

        retrieved_task = await repo.get_task_by_id("non-existent-id")

        assert retrieved_task is None

    @pytest.mark.asyncio
    async def test_get_child_tasks_by_parent_id(self, sync_db_session):
        """Test retrieving child tasks by parent ID"""
        repo = TaskRepository(sync_db_session)

        # Create parent task
        parent = await repo.create_task(name="Parent Task", user_id="test-user")

        # Create child tasks
        child1 = await repo.create_task(name="Child 1", user_id="test-user", parent_id=parent.id)

        child2 = await repo.create_task(name="Child 2", user_id="test-user", parent_id=parent.id)

        # Retrieve children
        children = await repo.get_child_tasks_by_parent_id(parent.id)

        assert len(children) == 2
        child_ids = [c.id for c in children]
        assert child1.id in child_ids
        assert child2.id in child_ids

    @pytest.mark.asyncio
    async def test_update_task_status(self, sync_db_session):
        """Test updating task status"""
        repo = TaskRepository(sync_db_session)

        # Create a task
        task = await repo.create_task(name="Test Task", user_id="test-user")

        assert task.status == "pending"

        # Update status
        await repo.update_task(
            task_id=task.id, status="completed", result={"output": "result"}, progress=1.0
        )

        # Retrieve and verify
        updated_task = await repo.get_task_by_id(task.id)
        assert updated_task.status == "completed"
        assert updated_task.result == {"output": "result"}
        assert float(updated_task.progress) == 1.0

    @pytest.mark.asyncio
    async def test_update_task_inputs(self, sync_db_session):
        """Test updating task input data"""
        repo = TaskRepository(sync_db_session)

        # Create a task
        task = await repo.create_task(
            name="Test Task", user_id="test-user", inputs={"initial": "data"}
        )

        # Update input data
        new_input = {"updated": "data", "url": "https://example.com"}
        await repo.update_task(task.id, inputs=new_input)

        # Retrieve and verify
        updated_task = await repo.get_task_by_id(task.id)
        assert updated_task.inputs == new_input

    @pytest.mark.asyncio
    async def test_get_root_task(self, sync_db_session):
        """Test retrieving root task from a task tree"""
        repo = TaskRepository(sync_db_session)

        # Create task tree
        root = await repo.create_task(name="Root Task", user_id="test-user")

        child = await repo.create_task(name="Child Task", user_id="test-user", parent_id=root.id)

        grandchild = await repo.create_task(
            name="Grandchild Task", user_id="test-user", parent_id=child.id
        )

        # Get root from grandchild (pass task object, not task id)
        retrieved_root = await repo.get_root_task(grandchild)

        assert retrieved_root is not None
        assert retrieved_root.id == root.id

    @pytest.mark.asyncio
    async def test_get_all_children_recursive(self, sync_db_session):
        """Test recursively getting all children tasks"""
        repo = TaskRepository(sync_db_session)

        # Create task tree: root -> child1, child2 -> grandchild1, grandchild2
        root = await repo.create_task(name="Root Task", user_id="test-user")

        child1 = await repo.create_task(name="Child 1", user_id="test-user", parent_id=root.id)

        child2 = await repo.create_task(name="Child 2", user_id="test-user", parent_id=root.id)

        grandchild1 = await repo.create_task(
            name="Grandchild 1", user_id="test-user", parent_id=child1.id
        )

        grandchild2 = await repo.create_task(
            name="Grandchild 2", user_id="test-user", parent_id=child1.id
        )

        # Get all children recursively
        all_children = await repo.get_all_children_recursive(root.id)

        # Should get child1, child2, grandchild1, grandchild2
        assert len(all_children) == 4
        child_ids = [c.id for c in all_children]
        assert child1.id in child_ids
        assert child2.id in child_ids
        assert grandchild1.id in child_ids
        assert grandchild2.id in child_ids

    @pytest.mark.asyncio
    async def test_get_all_children_recursive_empty(self, sync_db_session):
        """Test getting children for a task with no children"""
        repo = TaskRepository(sync_db_session)

        root = await repo.create_task(name="Root Task", user_id="test-user")

        all_children = await repo.get_all_children_recursive(root.id)

        assert len(all_children) == 0

    @pytest.mark.asyncio
    async def test_delete_task(self, sync_db_session):
        """Test physically deleting a task"""
        repo = TaskRepository(sync_db_session)

        # Create a task
        task = await repo.create_task(name="Task to Delete", user_id="test-user")

        task_id = task.id

        # Verify task exists
        retrieved = await repo.get_task_by_id(task_id)
        assert retrieved is not None

        # Delete the task
        result = await repo.delete_task(task_id)
        assert result is True

        # Verify task is deleted
        deleted_task = await repo.get_task_by_id(task_id)
        assert deleted_task is None

    @pytest.mark.asyncio
    async def test_delete_task_not_found(self, sync_db_session):
        """Test deleting a non-existent task"""
        repo = TaskRepository(sync_db_session)

        result = await repo.delete_task("non-existent-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_save_task_tree_persists_grandchildren(self, sync_db_session):
        """Test that save_task_tree persists nodes at depth >= 3 (grandchildren)"""
        repo = TaskRepository(sync_db_session)

        root_id = str(uuid.uuid4())
        child_id = str(uuid.uuid4())
        grandchild_id = str(uuid.uuid4())

        root_node = TaskTreeNode(task=repo.build_task(id=root_id, name="Root", user_id="test-user"))
        child_node = TaskTreeNode(
            task=repo.build_task(id=child_id, name="Child", user_id="test-user")
        )
        grandchild_node = TaskTreeNode(
            task=repo.build_task(id=grandchild_id, name="Grandchild", user_id="test-user")
        )

        child_node.add_child(grandchild_node)
        root_node.add_child(child_node)

        result = await repo.save_task_tree(root_node)
        assert result is True

        saved_child = await repo.get_task_by_id(child_id)
        assert saved_child is not None
        assert saved_child.parent_id == root_id

        saved_grandchild = await repo.get_task_by_id(grandchild_id)
        assert saved_grandchild is not None
        assert saved_grandchild.parent_id == child_id
