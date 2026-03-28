# --- Origin Type Tests (from test_task_creator_origin_types.py, deduplicated) ---
import pytest
from apflow.core.storage.sqlalchemy.models import TaskOriginType
from apflow.core.execution.task_creator import TaskCreator
from apflow.core.types import TaskTreeNode
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

from apflow.logger import get_logger

logger = get_logger(__name__)


class TestTaskCreatorOriginTypes:
    """Test TaskCreator origin type methods: from_link, from_copy, from_archive, from_mixed"""

    @pytest.mark.asyncio
    async def test_from_link_fails_if_not_completed(self, sync_db_session):
        """Linking a task tree with any non-completed node raises ValueError, but works if all completed"""
        task_repository = TaskRepository(sync_db_session)
        creator = TaskCreator(sync_db_session)
        root = await task_repository.create_task(
            name="Root Task",
            user_id="user_123",
            status="completed",
        )
        child = await task_repository.create_task(
            name="Child Task",
            user_id="user_123",
            parent_id=root.id,
            status="completed",
        )

        root.has_children = True
        logger.info(f"Created tasks: root={root.output()}, child={child.output()}")
        sync_db_session.commit()
        await creator.from_link(_original_task=root, _save=True, _recursive=True)
        child.status = "pending"
        sync_db_session.commit()
        sync_db_session.refresh(child)
        assert child.status == "pending"
        with pytest.raises(ValueError, match="Only a fully completed task tree can be linked"):
            await creator.from_link(_original_task=root, _save=True, _recursive=True)
        child.status = "completed"
        sync_db_session.commit()
        result = await creator.from_link(_original_task=root, _save=True, _recursive=True)
        assert isinstance(result, TaskTreeNode)
        assert result.task.status == "completed"
        for c in result.children:
            assert c.task.status == "completed"

    @pytest.mark.asyncio
    async def test_from_link_single_task_and_overrides(self, sync_db_session):
        """Test linking a single completed task, with and without field overrides"""
        task_repository = TaskRepository(sync_db_session)
        creator = TaskCreator(sync_db_session)
        original_task = await task_repository.create_task(
            name="Original Task",
            user_id="user_123",
            priority=2,
            status="completed",
        )
        sync_db_session.commit()
        sync_db_session.refresh(original_task)
        original_task.status = "completed"
        sync_db_session.commit()
        sync_db_session.refresh(original_task)
        linked_task = await creator.from_link(
            _original_task=original_task, _save=True, _recursive=False
        )
        assert isinstance(linked_task, TaskTreeNode)
        assert linked_task.task.origin_type == TaskOriginType.link
        assert linked_task.task.original_task_id == original_task.id
        assert linked_task.task.name == original_task.name
        assert linked_task.task.user_id == original_task.user_id
        # With overrides
        linked_task2 = await creator.from_link(
            _original_task=original_task, _save=True, _recursive=False, priority=0
        )
        assert linked_task2.task.priority == 0

    @pytest.mark.asyncio
    async def test_from_link_recursive_tree(self, sync_db_session):
        """Test linking entire task tree recursively, children are linked"""
        task_repository = TaskRepository(sync_db_session)
        creator = TaskCreator(sync_db_session)
        root = await task_repository.create_task(
            name="Root Task",
            user_id="user_123",
            priority=1,
            status="completed",
        )
        child1 = await task_repository.create_task(
            name="Child 1",
            user_id="user_123",
            parent_id=root.id,
            priority=2,
            status="completed",
        )
        child2 = await task_repository.create_task(
            name="Child 2",
            user_id="user_123",
            parent_id=root.id,
            status="completed",
            dependencies=[{"id": child1.id, "required": True}],
        )
        root.status = child1.status = child2.status = "completed"
        root.has_children = True
        sync_db_session.commit()
        sync_db_session.refresh(root)
        sync_db_session.refresh(child1)
        sync_db_session.refresh(child2)
        linked_tree = await creator.from_link(_original_task=root, _save=True, _recursive=True)
        assert isinstance(linked_tree, TaskTreeNode)
        assert linked_tree.task.origin_type == TaskOriginType.link
        assert linked_tree.task.original_task_id == root.id
        assert len(linked_tree.children) == 2
        for child_node in linked_tree.children:
            assert child_node.task.origin_type == TaskOriginType.link
            assert child_node.task.parent_id == linked_tree.task.id

    @pytest.mark.asyncio
    async def test_user_id_error_from_link(self, sync_db_session):
        """Test all tasks must have same user_id or none"""
        task_repository = TaskRepository(sync_db_session)
        creator = TaskCreator(sync_db_session)
        root = await task_repository.create_task(
            name="Root Task",
            user_id="user_123",
            priority=1,
            status="completed",
        )
        child1 = await task_repository.create_task(
            name="Child 1",
            user_id="user_123",
            parent_id=root.id,
            priority=2,
            status="completed",
        )
        child2 = await task_repository.create_task(
            name="Child 2",
            user_id="user_123",
            parent_id=root.id,
            status="completed",
            dependencies=[{"id": child1.id, "required": True}],
        )

        root.status = child1.status = child2.status = "completed"
        root.has_children = True
        sync_db_session.commit()
        sync_db_session.refresh(root)
        sync_db_session.refresh(child1)
        sync_db_session.refresh(child2)
        with pytest.raises(ValueError, match="Deny linking to a different user's task."):
            await creator.from_link(
                _original_task=root, _save=True, _recursive=True, user_id="different_user"
            )

    @pytest.mark.asyncio
    async def test_from_copy_single_task_and_overrides(self, sync_db_session):
        """Test copying a single task, with and without field overrides"""
        task_repository = TaskRepository(sync_db_session)
        creator = TaskCreator(sync_db_session)
        original_task = await task_repository.create_task(
            name="Original Task",
            user_id="user_123",
            priority=2,
            status="completed",
        )
        copied_task = await creator.from_copy(
            _original_task=original_task, _save=True, _recursive=False
        )
        assert isinstance(copied_task, TaskTreeNode)
        assert copied_task.task.origin_type == TaskOriginType.copy
        assert copied_task.task.original_task_id == original_task.id
        # With overrides
        copied_task2 = await creator.from_copy(
            _original_task=original_task,
            _save=True,
            _recursive=False,
            user_id="new_user_456",
            priority=1,
        )
        assert copied_task2.task.user_id == "new_user_456"
        assert copied_task2.task.priority == 1

    @pytest.mark.asyncio
    async def test_from_copy_recursive_tree(self, sync_db_session):
        """Test copying entire task tree recursively, children are copied"""
        task_repository = TaskRepository(sync_db_session)
        creator = TaskCreator(sync_db_session)
        root = await task_repository.create_task(
            name="Root Task",
            user_id="user_123",
            priority=1,
        )
        child1 = await task_repository.create_task(
            name="Child 1",
            user_id="user_123",
            parent_id=root.id,
            priority=2,
        )
        await task_repository.create_task(
            name="Child 2",
            user_id="user_123",
            parent_id=root.id,
            dependencies=[{"id": child1.id, "required": True}],
        )
        root.has_children = True
        sync_db_session.commit()
        copied_tree = await creator.from_copy(_original_task=root, _save=True, _recursive=True)
        assert isinstance(copied_tree, TaskTreeNode)
        assert copied_tree.task.origin_type == TaskOriginType.copy
        assert copied_tree.task.original_task_id == root.id
        assert len(copied_tree.children) == 2
        for child_node in copied_tree.children:
            assert child_node.task.origin_type == TaskOriginType.copy
            assert child_node.task.parent_id == copied_tree.task.id

    @pytest.mark.asyncio
    async def test_from_archive_single_task(self, sync_db_session):
        """Test archiving a single task"""
        task_repository = TaskRepository(sync_db_session)
        creator = TaskCreator(sync_db_session)
        completed_task = await task_repository.create_task(
            name="Completed Task",
            user_id="user_123",
            priority=2,
            status="completed",
        )
        archive_task = await creator.from_archive(
            _original_task=completed_task, _save=True, _recursive=False
        )
        assert archive_task.task.origin_type == TaskOriginType.archive
        assert archive_task.task.name == completed_task.name
        assert archive_task.task.status == completed_task.status

    @pytest.mark.asyncio
    async def test_from_archive_recursive_tree(self, sync_db_session):
        """Test archiving entire task tree"""
        task_repository = TaskRepository(sync_db_session)
        creator = TaskCreator(sync_db_session)
        root = await task_repository.create_task(
            name="Root Task",
            user_id="user_123",
            status="completed",
        )
        await task_repository.create_task(
            name="Child Task",
            user_id="user_123",
            parent_id=root.id,
            status="completed",
        )
        root.has_children = True
        sync_db_session.commit()
        archive_tree = await creator.from_archive(_original_task=root, _save=True, _recursive=True)
        assert isinstance(archive_tree, TaskTreeNode)
        assert archive_tree.task.origin_type == TaskOriginType.archive
        assert archive_tree.task.id == root.id
        assert archive_tree.task.original_task_id == root.original_task_id
        assert len(archive_tree.children) == 1

    @pytest.mark.asyncio
    async def test_from_mixed_link_and_copy(self, sync_db_session):
        """Test mixed mode: link some, copy others"""
        task_repository = TaskRepository(sync_db_session)
        creator = TaskCreator(sync_db_session)
        root = await task_repository.create_task(
            name="Root Task",
            user_id="user_123",
            priority=1,
        )
        child1 = await task_repository.create_task(
            name="Child 1",
            user_id="user_123",
            parent_id=root.id,
            priority=2,
        )
        await task_repository.create_task(
            name="Child 2",
            user_id="user_123",
            parent_id=root.id,
            dependencies=[{"id": child1.id, "required": True}],
        )
        root.has_children = True
        child1.has_children = True
        sync_db_session.commit()
        mixed_tree = await creator.from_mixed(
            _original_task=root, _save=True, _recursive=True, _link_task_ids=[root.id, child1.id]
        )
        assert isinstance(mixed_tree, TaskTreeNode)
        assert mixed_tree.task.origin_type == TaskOriginType.link
        origin_types = {mixed_tree.task.origin_type}
        for child in mixed_tree.children:
            origin_types.add(child.task.origin_type)
        assert len(origin_types) >= 1

    @pytest.mark.asyncio
    async def test_origin_type_immutability(self, sync_db_session):
        """Test that origin_type is set correctly for each operation"""
        task_repository = TaskRepository(sync_db_session)
        creator = TaskCreator(sync_db_session)
        original = await task_repository.create_task(
            name="Original",
            user_id="user_123",
            status="completed",
        )
        original.status = "completed"
        sync_db_session.commit()
        linked = await creator.from_link(_original_task=original, _recursive=False)
        assert linked.task.origin_type == TaskOriginType.link
        copied = await creator.from_copy(_original_task=original, _recursive=False)
        assert copied.task.origin_type == TaskOriginType.copy
        archive = await creator.from_archive(_original_task=original, _recursive=False)
        assert archive.task.origin_type == TaskOriginType.archive

    @pytest.mark.asyncio
    async def test_user_id_error_from_mixed(self, sync_db_session):
        """Test all tasks must have same user_id or none"""
        task_repository = TaskRepository(sync_db_session)
        creator = TaskCreator(sync_db_session)
        root = await task_repository.create_task(
            name="Root Task",
            user_id="user_123",
            priority=1,
            status="completed",
        )
        child1 = await task_repository.create_task(
            name="Child 1",
            user_id="user_123",
            parent_id=root.id,
            priority=2,
            status="completed",
        )
        child2 = await task_repository.create_task(
            name="Child 2",
            user_id="user_123",
            parent_id=root.id,
            status="completed",
            dependencies=[{"id": child1.id, "required": True}],
        )

        root.status = child1.status = child2.status = "completed"
        root.has_children = True
        sync_db_session.commit()
        sync_db_session.refresh(root)
        sync_db_session.refresh(child1)
        sync_db_session.refresh(child2)

        # Test recursive
        with pytest.raises(ValueError, match="Deny linking to a different user's task."):
            await creator.from_mixed(
                _original_task=root,
                _save=True,
                _recursive=True,
                user_id="different_user",
                _link_task_ids=[root.id, child1.id],
            )

        # Test non-recursive
        with pytest.raises(ValueError, match="Deny linking to a different user's task."):
            await creator.from_mixed(
                _original_task=root,
                _save=True,
                _recursive=False,
                user_id="different_user",
                _link_task_ids=[root.id],
            )


"""
Test TaskCreator functionality
"""


class TestTaskCreator:
    """Test TaskCreator core functionality"""

    @pytest.mark.asyncio
    async def test_create_task_tree_with_id(self, sync_db_session):
        """Test creating task tree with id-based references"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_1",
                "name": "Task 1",
                "user_id": "user_123",
                "priority": 1,
                "inputs": {"url": "https://example.com"},
            },
            {
                "id": "task_2",
                "name": "Task 2",
                "user_id": "user_123",
                "parent_id": "task_1",
                "dependencies": [{"id": "task_1", "required": True}],
            },
        ]

        task_tree = await creator.create_task_tree_from_array(tasks)

        assert isinstance(task_tree, TaskTreeNode)
        assert task_tree.task.name == "Task 1"
        assert task_tree.task.task_tree_id == "task_1"
        assert len(task_tree.children) == 1
        assert task_tree.children[0].task.name == "Task 2"
        assert task_tree.children[0].task.task_tree_id == "task_1"

        # Verify parent_id is set correctly
        child_task = task_tree.children[0].task
        assert child_task.parent_id == task_tree.task.id

        # Verify dependencies are set correctly (using actual task ids)
        assert child_task.dependencies is not None
        assert len(child_task.dependencies) == 1
        assert child_task.dependencies[0]["id"] == task_tree.task.id
        assert child_task.dependencies[0]["required"] is True

    @pytest.mark.asyncio
    async def test_create_task_tree_with_name(self, sync_db_session):
        """Test creating task tree with name-based references (no id)"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "name": "Task 1",  # No id, name must be unique
                "user_id": "user_123",
                "priority": 1,
            },
            {
                "name": "Task 2",  # No id, name must be unique
                "user_id": "user_123",
                "parent_id": "Task 1",  # Use name as parent_id
                "dependencies": [{"name": "Task 1", "required": True}],  # Use name in dependencies
            },
        ]

        task_tree = await creator.create_task_tree_from_array(tasks)

        assert isinstance(task_tree, TaskTreeNode)
        assert task_tree.task.name == "Task 1"
        assert len(task_tree.children) == 1
        assert task_tree.children[0].task.name == "Task 2"

        # Verify parent_id is set correctly (using actual task id)
        child_task = task_tree.children[0].task
        assert child_task.parent_id == task_tree.task.id

        # Verify dependencies are set correctly (using actual task ids)
        assert child_task.dependencies is not None
        assert len(child_task.dependencies) == 1
        assert child_task.dependencies[0]["id"] == task_tree.task.id
        assert child_task.dependencies[0]["required"] is True

    @pytest.mark.asyncio
    async def test_multiple_root_allowed(self, sync_db_session):
        """Multi-root creation is now allowed in v2."""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_1",
                "name": "Task 1",
                "user_id": "user_123",
            },
            {
                "id": "task_2",
                "name": "Task 2",
                "user_id": "user_123",
            },
        ]

        result = await creator.create_task_tree_from_array(tasks)
        assert result is not None
        assert result.task.name == "Task 1"

    @pytest.mark.asyncio
    async def test_create_task_tree_multiple_levels(self, sync_db_session):
        """Test creating task tree with multiple levels"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "root",
                "name": "Root Task",
                "user_id": "user_123",
            },
            {
                "id": "child_1",
                "name": "Child 1",
                "user_id": "user_123",
                "parent_id": "root",
            },
            {
                "id": "child_2",
                "name": "Child 2",
                "user_id": "user_123",
                "parent_id": "root",
            },
            {
                "id": "grandchild",
                "name": "Grandchild",
                "user_id": "user_123",
                "parent_id": "child_1",
                "dependencies": [{"id": "child_2", "required": True}],
            },
        ]

        task_tree = await creator.create_task_tree_from_array(tasks)

        assert task_tree.task.name == "Root Task"
        assert len(task_tree.children) == 2

        # Find child_1
        child_1 = next((c for c in task_tree.children if c.task.name == "Child 1"), None)
        assert child_1 is not None
        assert len(child_1.children) == 1
        assert child_1.children[0].task.name == "Grandchild"

        # Verify grandchild's dependencies
        grandchild = child_1.children[0].task
        assert grandchild.dependencies is not None
        assert len(grandchild.dependencies) == 1

    @pytest.mark.asyncio
    async def test_create_task_tree_without_parent_id(self, sync_db_session):
        """Test creating task tree without parent_id (root task)"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_1",
                "name": "Task 1",
                "user_id": "user_123",
                # No parent_id - this is a root task
            }
        ]

        task_tree = await creator.create_task_tree_from_array(tasks)

        # Root task should have no parent_id
        assert task_tree.task.parent_id is None
        assert task_tree.task.name == "Task 1"

    @pytest.mark.asyncio
    async def test_error_missing_name(self, sync_db_session):
        """Test error when task is missing name"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_1",
                # Missing name
                "user_id": "user_123",
            }
        ]

        with pytest.raises(ValueError, match="must have a 'name' field"):
            await creator.create_task_tree_from_array(tasks)

    @pytest.mark.asyncio
    async def test_create_task_tree_without_user_id(self, sync_db_session):
        """Test creating task tree without user_id (user_id is optional)"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_1",
                "name": "Task 1",
                # No user_id - should work
            },
            {
                "id": "task_2",
                "name": "Task 2",
                "parent_id": "task_1",
            },
        ]

        task_tree = await creator.create_task_tree_from_array(tasks)

        assert task_tree.task.name == "Task 1"
        assert task_tree.task.user_id is None
        assert len(task_tree.children) == 1
        assert task_tree.children[0].task.user_id is None

    @pytest.mark.asyncio
    async def test_error_duplicate_id(self, sync_db_session):
        """Test error when duplicate id is provided in the same array"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_1",
                "name": "Task 1",
                "user_id": "user_123",
            },
            {
                "id": "task_1_1",  # Duplicate id in same array
                "name": "Task 1_1",
                "user_id": "user_123",
                "parent_id": "task_1",
            },
            {
                "id": "task_1_1",  # Duplicate id in same array
                "name": "Task 1_2",
                "user_id": "user_123",
                "parent_id": "task_1",
            },
        ]

        with pytest.raises(ValueError, match="Duplicate task id"):
            await creator.create_task_tree_from_array(tasks)

    @pytest.mark.asyncio
    async def test_error_id_when_exists_in_db(self, sync_db_session):
        """Test error that when provided ID already exists in database"""
        creator = TaskCreator(sync_db_session)

        # First, create a task with a specific ID
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
        from apflow.core.config import get_task_model_class

        repo = TaskRepository(sync_db_session, task_model_class=get_task_model_class())
        existing_task = await repo.create_task(
            name="Existing Task", user_id="user_123", id="task_1"
        )
        assert existing_task.id == "task_1"

        # Now try to create a new task tree with the same ID
        # System should auto-generate a new UUID to avoid conflict
        tasks = [
            {
                "id": "task_1",  # This ID already exists in DB
                "name": "New Task 1",
                "user_id": "user_123",
            },
            {
                "id": "task_2",
                "name": "New Task 2",
                "user_id": "user_123",
                "parent_id": "task_1",  # References task_1
            },
        ]

        with pytest.raises(ValueError, match="already exists in database"):
            await creator.create_task_tree_from_array(tasks)

    @pytest.mark.asyncio
    async def test_error_duplicate_name_without_id(self, sync_db_session):
        """Test error when duplicate name is provided (when no id)"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "name": "Task 1",  # No id
                "user_id": "user_123",
            },
            {
                "name": "Task 1",  # Duplicate name (no id)
                "user_id": "user_123",
            },
        ]

        with pytest.raises(ValueError, match="Duplicate task name"):
            await creator.create_task_trees_from_array(tasks)

    @pytest.mark.asyncio
    async def test_error_invalid_parent_id(self, sync_db_session):
        """Test error when parent_id doesn't exist in array"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_1",
                "name": "Task 1",
                "user_id": "user_123",
            },
            {
                "id": "task_2",
                "name": "Task 2",
                "user_id": "user_123",
                "parent_id": "nonexistent_task",  # Invalid parent_id
            },
        ]

        with pytest.raises(ValueError, match="which is not in the task array"):
            await creator.create_task_tree_from_array(tasks)

    @pytest.mark.asyncio
    async def test_error_invalid_dependency_id(self, sync_db_session):
        """Test error when dependency id doesn't exist in array"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_1",
                "name": "Task 1",
                "user_id": "user_123",
            },
            {
                "id": "task_2",
                "name": "Task 2",
                "user_id": "user_123",
                "dependencies": [{"id": "nonexistent_task", "required": True}],
            },
        ]

        with pytest.raises(
            ValueError, match="dependency.*which is not a valid id or name in the task array"
        ):
            await creator.create_task_trees_from_array(tasks)

    @pytest.mark.asyncio
    async def test_error_invalid_dependency_name(self, sync_db_session):
        """Test error when dependency name doesn't exist in array"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "name": "Task 1",  # No id
                "user_id": "user_123",
            },
            {
                "name": "Task 2",  # No id
                "user_id": "user_123",
                "dependencies": [{"name": "nonexistent_task", "required": True}],
            },
        ]

        with pytest.raises(
            ValueError, match="dependency.*which is not a valid id or name in the task array"
        ):
            await creator.create_task_trees_from_array(tasks)

    @pytest.mark.asyncio
    async def test_error_dependency_missing_id_and_name(self, sync_db_session):
        """Test error when dependency has neither id nor name"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_1",
                "name": "Task 1",
                "user_id": "user_123",
            },
            {
                "id": "task_2",
                "name": "Task 2",
                "user_id": "user_123",
                "dependencies": [{"required": True}],  # Missing id and name
            },
        ]

        with pytest.raises(ValueError, match="not a valid id or name in the task array"):
            await creator.create_task_trees_from_array(tasks)

    @pytest.mark.asyncio
    async def test_error_empty_tasks_array(self, sync_db_session):
        """Test error when tasks array is empty"""
        creator = TaskCreator(sync_db_session)

        with pytest.raises(ValueError, match="Tasks array cannot be empty"):
            await creator.create_task_tree_from_array([])

    @pytest.mark.asyncio
    async def test_dependencies_with_required_and_type(self, sync_db_session):
        """Test dependencies with required and type fields"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_1",
                "name": "Task 1",
                "user_id": "user_123",
            },
            {
                "id": "task_2",
                "name": "Task 2",
                "user_id": "user_123",
                "parent_id": "task_1",  # Task 2 is a child of Task 1
                "dependencies": [{"id": "task_1", "required": False, "type": "result"}],
            },
        ]

        task_tree = await creator.create_task_tree_from_array(tasks)

        child_task = task_tree.children[0].task
        assert child_task.dependencies is not None
        assert len(child_task.dependencies) == 1
        assert child_task.dependencies[0]["required"] is False
        assert child_task.dependencies[0]["type"] == "result"

    @pytest.mark.asyncio
    async def test_simple_string_dependency(self, sync_db_session):
        """Test simple string dependency (backward compatibility)"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_1",
                "name": "Task 1",
                "user_id": "user_123",
            },
            {
                "id": "task_2",
                "name": "Task 2",
                "user_id": "user_123",
                "parent_id": "task_1",  # Task 2 is a child of Task 1
                "dependencies": ["task_1"],  # Simple string dependency
            },
        ]

        task_tree = await creator.create_task_tree_from_array(tasks)

        child_task = task_tree.children[0].task
        assert child_task.dependencies is not None
        assert len(child_task.dependencies) == 1
        assert child_task.dependencies[0] == task_tree.task.id

    @pytest.mark.asyncio
    async def test_parent_has_children_flag(self, sync_db_session):
        """Test that parent task's has_children flag is set correctly"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "parent",
                "name": "Parent",
                "user_id": "user_123",
            },
            {
                "id": "child",
                "name": "Child",
                "user_id": "user_123",
                "parent_id": "parent",
            },
        ]

        task_tree = await creator.create_task_tree_from_array(tasks)

        # Refresh parent task from database to get updated has_children flag
        parent_task = await creator.task_repository.get_task_by_id(task_tree.task.id)
        assert parent_task.has_children is True

    @pytest.mark.asyncio
    async def test_multiple_children_same_parent(self, sync_db_session):
        """Test multiple children with same parent"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "parent",
                "name": "Parent",
                "user_id": "user_123",
            },
            {
                "id": "child_1",
                "name": "Child 1",
                "user_id": "user_123",
                "parent_id": "parent",
            },
            {
                "id": "child_2",
                "name": "Child 2",
                "user_id": "user_123",
                "parent_id": "parent",
            },
            {
                "id": "child_3",
                "name": "Child 3",
                "user_id": "user_123",
                "parent_id": "parent",
            },
        ]

        task_tree = await creator.create_task_tree_from_array(tasks)

        assert len(task_tree.children) == 3
        assert all(child.task.parent_id == task_tree.task.id for child in task_tree.children)

    @pytest.mark.asyncio
    async def test_dependencies_use_actual_task_ids(self, sync_db_session):
        """Test that dependencies use actual task ids (user-provided or system-generated)"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "user_id_1",  # User-provided id
                "name": "Task 1",
                "user_id": "user_123",
            },
            {
                "id": "user_id_2",  # User-provided id
                "name": "Task 2",
                "user_id": "user_123",
                "parent_id": "user_id_1",  # Task 2 is a child of Task 1
                "dependencies": [{"id": "user_id_1", "required": True}],
            },
        ]

        task_tree = await creator.create_task_tree_from_array(tasks)

        # Dependencies should use actual task id (user-provided id if provided, otherwise system-generated)
        child_task = task_tree.children[0].task
        assert child_task.dependencies is not None
        assert len(child_task.dependencies) == 1

        # The dependency id should be the actual task id
        # If user provided id, use it; otherwise use system-generated UUID
        actual_dep_id = child_task.dependencies[0]["id"]
        assert actual_dep_id == task_tree.task.id  # Actual task id
        # Since user provided id="user_id_1", the actual task id is "user_id_1"
        assert actual_dep_id == "user_id_1"  # User-provided id becomes the actual task id

    @pytest.mark.asyncio
    async def test_error_circular_dependency_simple(self, sync_db_session):
        """Test error when simple circular dependency is detected (A -> B -> A)"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_a",
                "name": "Task A",
                "user_id": "user_123",
                # Root task
                "dependencies": [{"id": "task_b", "required": True}],
            },
            {
                "id": "task_b",
                "name": "Task B",
                "user_id": "user_123",
                "parent_id": "task_a",  # Child of task_a, in the same tree
                "dependencies": [{"id": "task_a", "required": True}],
            },
        ]

        with pytest.raises(ValueError, match="Circular dependency detected"):
            await creator.create_task_tree_from_array(tasks)

    @pytest.mark.asyncio
    async def test_error_circular_dependency_three_nodes(self, sync_db_session):
        """Test error when circular dependency involves three nodes (A -> B -> C -> A)"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_a",
                "name": "Task A",
                "user_id": "user_123",
                # Root task
                "dependencies": [{"id": "task_b", "required": True}],
            },
            {
                "id": "task_b",
                "name": "Task B",
                "user_id": "user_123",
                "parent_id": "task_a",  # Child of task_a, in the same tree
                "dependencies": [{"id": "task_c", "required": True}],
            },
            {
                "id": "task_c",
                "name": "Task C",
                "user_id": "user_123",
                "parent_id": "task_b",  # Child of task_b, in the same tree
                "dependencies": [{"id": "task_a", "required": True}],
            },
        ]

        with pytest.raises(ValueError, match="Circular dependency detected"):
            await creator.create_task_tree_from_array(tasks)

    @pytest.mark.asyncio
    async def test_error_circular_dependency_with_name(self, sync_db_session):
        """Test error when circular dependency is detected using name-based references"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "name": "Task A",  # No id, use name
                "user_id": "user_123",
                # Root task
                "dependencies": [{"name": "Task B", "required": True}],
            },
            {
                "name": "Task B",  # No id, use name
                "user_id": "user_123",
                "parent_id": "Task A",  # Child of Task A, in the same tree
                "dependencies": [{"name": "Task A", "required": True}],
            },
        ]

        with pytest.raises(ValueError, match="Circular dependency detected"):
            await creator.create_task_tree_from_array(tasks)

    @pytest.mark.asyncio
    async def test_error_circular_dependency_self_reference(self, sync_db_session):
        """Test error when task depends on itself"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_a",
                "name": "Task A",
                "user_id": "user_123",
                "dependencies": [{"id": "task_a", "required": True}],  # Self-reference
            }
        ]

        with pytest.raises(ValueError, match="Circular dependency detected"):
            await creator.create_task_tree_from_array(tasks)

    @pytest.mark.asyncio
    async def test_error_circular_dependency_complex(self, sync_db_session):
        """Test error when circular dependency involves multiple paths"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_a",
                "name": "Task A",
                "user_id": "user_123",
                # Root task
                "dependencies": [{"id": "task_b", "required": True}],
            },
            {
                "id": "task_b",
                "name": "Task B",
                "user_id": "user_123",
                "parent_id": "task_a",  # Child of task_a, in the same tree
                "dependencies": [
                    {"id": "task_c", "required": True},
                    {"id": "task_d", "required": True},
                ],
            },
            {
                "id": "task_c",
                "name": "Task C",
                "user_id": "user_123",
                "parent_id": "task_b",  # Child of task_b, in the same tree
                "dependencies": [
                    {"id": "task_a", "required": True}
                ],  # Creates cycle: A -> B -> C -> A
            },
            {
                "id": "task_d",
                "name": "Task D",
                "user_id": "user_123",
                "parent_id": "task_b",  # Child of task_b, in the same tree
                "dependencies": [{"id": "task_b", "required": True}],  # Creates cycle: B -> D -> B
            },
        ]

        with pytest.raises(ValueError, match="Circular dependency detected"):
            await creator.create_task_tree_from_array(tasks)

    @pytest.mark.asyncio
    async def test_error_circular_dependency_simple_string(self, sync_db_session):
        """Test error when circular dependency uses simple string format"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_a",
                "name": "Task A",
                "user_id": "user_123",
                # Root task
                "dependencies": ["task_b"],  # Simple string format
            },
            {
                "id": "task_b",
                "name": "Task B",
                "user_id": "user_123",
                "parent_id": "task_a",  # Child of task_a, in the same tree
                "dependencies": ["task_a"],  # Simple string format
            },
        ]

        with pytest.raises(ValueError, match="Circular dependency detected"):
            await creator.create_task_tree_from_array(tasks)

    @pytest.mark.asyncio
    async def test_no_error_valid_dependency_chain(self, sync_db_session):
        """Test that valid dependency chain (no cycles) works correctly"""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_a",
                "name": "Task A",
                "user_id": "user_123",
                # No dependencies - root task
            },
            {
                "id": "task_b",
                "name": "Task B",
                "user_id": "user_123",
                "parent_id": "task_a",  # Child of task_a
                "dependencies": [{"id": "task_a", "required": True}],
            },
            {
                "id": "task_c",
                "name": "Task C",
                "user_id": "user_123",
                "parent_id": "task_a",  # Child of task_a
                "dependencies": [
                    {"id": "task_a", "required": True},
                    {"id": "task_b", "required": True},
                ],
            },
        ]

        # Should not raise error - valid dependency chain (no cycles)
        task_tree = await creator.create_task_tree_from_array(tasks)

        assert task_tree.task.name == "Task A"
        assert len(task_tree.children) == 2  # task_b and task_c are children of task_a

        # Verify dependencies are set correctly
        task_b = next((t for t in task_tree.to_list() if t.name == "Task B"), None)
        task_c = next((t for t in task_tree.to_list() if t.name == "Task C"), None)

        assert task_b is not None
        assert task_c is not None
        assert task_b.dependencies is not None
        assert len(task_b.dependencies) == 1
        assert task_c.dependencies is not None
        assert len(task_c.dependencies) == 2

    @pytest.mark.asyncio
    async def test_multi_root_tasks_allowed(self, sync_db_session):
        """Multi-root task forests are now allowed (v2 relaxation)."""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_a",
                "name": "Task A",
                "user_id": "user_123",
                # Root task 1
            },
            {
                "id": "task_b",
                "name": "Task B",
                "user_id": "user_123",
                # Root task 2
            },
            {
                "id": "task_c",
                "name": "Task C",
                "user_id": "user_123",
                "parent_id": "task_a",  # Child of task_a
            },
        ]

        # Should succeed — returns first root's tree
        result = await creator.create_task_tree_from_array(tasks)
        assert result is not None
        assert result.task.name == "Task A"

    @pytest.mark.asyncio
    async def test_multi_root_disconnected_trees_allowed(self, sync_db_session):
        """Disconnected trees (multi-root) are now allowed."""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_a",
                "name": "Task A",
                "user_id": "user_123",
            },
            {
                "id": "task_b",
                "name": "Task B",
                "user_id": "user_123",
                "parent_id": "task_a",
            },
            {
                "id": "task_c",
                "name": "Task C",
                "user_id": "user_123",
                "parent_id": "task_d",
            },
            {
                "id": "task_d",
                "name": "Task D",
                "user_id": "user_123",
                "parent_id": "task_e",
            },
            {
                "id": "task_e",
                "name": "Task E",
                "user_id": "user_123",
            },
        ]

        result = await creator.create_task_tree_from_array(tasks)
        assert result is not None

    @pytest.mark.asyncio
    async def test_disconnected_subtree_allowed(self, sync_db_session):
        """Disconnected subtrees (multi-root) are now allowed."""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_a",
                "name": "Task A",
                "user_id": "user_123",
            },
            {
                "id": "task_b",
                "name": "Task B",
                "user_id": "user_123",
                "parent_id": "task_a",
            },
            {
                "id": "task_c",
                "name": "Task C",
                "user_id": "user_123",
                "parent_id": "task_d",
            },
            {
                "id": "task_d",
                "name": "Task D",
                "user_id": "user_123",
            },
        ]

        result = await creator.create_task_tree_from_array(tasks)
        assert result is not None

    @pytest.mark.asyncio
    async def test_isolated_task_multi_root_allowed(self, sync_db_session):
        """Isolated tasks forming multi-root forests are now allowed."""
        creator = TaskCreator(sync_db_session)

        tasks = [
            {
                "id": "task_a",
                "name": "Task A",
                "user_id": "user_123",
            },
            {
                "id": "task_b",
                "name": "Task B",
                "user_id": "user_123",
                "parent_id": "task_a",
            },
            {
                "id": "task_c",
                "name": "Task C",
                "user_id": "user_123",
                "parent_id": "task_d",
            },
            {
                "id": "task_d",
                "name": "Task D",
                "user_id": "user_123",
                "parent_id": "task_e",
            },
            {
                "id": "task_e",
                "name": "Task E",
                "user_id": "user_123",
            },
        ]

        result = await creator.create_task_tree_from_array(tasks)
        assert result is not None
