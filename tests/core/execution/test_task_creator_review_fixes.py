"""Regression tests for task_creator review fixes (parent_id cycles, clone tree_id).

These exercise pure in-memory paths (validation + in-memory clone of non-link
tasks), so they run on a bare SQLite session without a live database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apflow.core.execution.task_creator import TaskCreator
from apflow.core.storage.sqlalchemy.models import TaskOriginType
from apflow.core.types import TaskTreeNode


def _creator() -> TaskCreator:
    session = sessionmaker(bind=create_engine("sqlite:///:memory:"))()
    return TaskCreator(session)


class TestParentIdCycleValidation:
    def test_self_parent_is_rejected(self) -> None:
        """Regression (CRITICAL): a self-parented task in a from_array payload
        used to persist (no FK) and later crash the DB tree walk with unbounded
        recursion. It must be rejected up front."""
        creator = _creator()
        tasks = [
            {"name": "root", "id": "R"},
            {"name": "loop", "id": "X", "parent_id": "X"},
        ]
        with pytest.raises(ValueError, match="parent_id cycle"):
            creator.task_dicts_to_task_models(tasks)

    def test_mutual_parent_cycle_is_rejected(self) -> None:
        creator = _creator()
        tasks = [
            {"name": "root", "id": "R"},
            {"name": "a", "id": "A", "parent_id": "B"},
            {"name": "b", "id": "B", "parent_id": "A"},
        ]
        with pytest.raises(ValueError, match="parent_id cycle"):
            creator.task_dicts_to_task_models(tasks)

    def test_valid_tree_is_accepted(self) -> None:
        creator = _creator()
        tasks = [
            {"name": "root", "id": "R"},
            {"name": "child", "id": "C", "parent_id": "R"},
        ]
        models = creator.task_dicts_to_task_models(tasks)
        assert len(models) == 2


class TestCloneResetsTreeId:
    @pytest.mark.asyncio
    async def test_clone_gets_fresh_task_tree_id(self) -> None:
        """Regression (CRITICAL): clone paths inherited the source's task_tree_id,
        so a tree_id-keyed load would merge source + clone. The clone must get a
        fresh task_tree_id rooted at its own new id."""
        creator = _creator()
        model_cls = creator.task_model_class

        root = model_cls.create({"id": "R", "name": "root", "task_tree_id": "R", "parent_id": None})
        child = model_cls.create(
            {"id": "C", "name": "child", "task_tree_id": "R", "parent_id": "R"}
        )
        root_node = TaskTreeNode(task=root)
        root_node.add_child(TaskTreeNode(task=child))

        cloned = await creator._clone_task_tree(root_node, {"origin_type": TaskOriginType.copy})

        new_root_id = str(cloned.task.id)
        assert new_root_id != "R"  # fresh id
        assert cloned.task.parent_id is None
        assert str(cloned.task.task_tree_id) == new_root_id
        # every descendant shares the new root's tree id, not the source "R"
        assert str(cloned.children[0].task.task_tree_id) == new_root_id
        assert str(cloned.children[0].task.task_tree_id) != "R"
