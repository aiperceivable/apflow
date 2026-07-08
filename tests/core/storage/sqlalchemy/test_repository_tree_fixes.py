"""Regression tests for task_repository tree-building review fixes.

These monkeypatch the DB-touching methods so the tree-assembly logic can be
exercised without a live database.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apflow.core.execution.errors import ValidationError
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


def _repo() -> TaskRepository:
    session = sessionmaker(bind=create_engine("sqlite:///:memory:"))()
    return TaskRepository(session)


@pytest.mark.asyncio
async def test_build_task_tree_falls_back_on_validation_error(monkeypatch) -> None:
    """Regression: the documented fast->slow fallback was dead because the
    except tuple omitted ValidationError (raised for 'Root task not found for
    tree_id'). It must now fall back instead of propagating."""
    repo = _repo()
    task = SimpleNamespace(id="R", task_tree_id="R", parent_id=None)

    async def raise_ve(tree_id):
        raise ValidationError(f"Root task not found for tree_id {tree_id}")

    async def no_children(parent_id):
        return []

    monkeypatch.setattr(repo, "build_task_tree_by_tree_id", raise_ve)
    monkeypatch.setattr(repo, "get_child_tasks_by_parent_id", no_children)

    tree = await repo.build_task_tree(task)  # must NOT raise
    assert tree.task is task


@pytest.mark.asyncio
async def test_build_task_tree_survives_parent_cycle(monkeypatch) -> None:
    """Regression (CRITICAL crash site): a self-parented row made the slow-path
    parent_id recursion loop forever (RecursionError). The cycle guard must stop it."""
    repo = _repo()
    # task_tree_id=None -> skips the fast path and exercises the slow recursion.
    task = SimpleNamespace(id="X", task_tree_id=None, parent_id="X")

    async def self_as_child(parent_id):
        return [task] if parent_id == "X" else []

    monkeypatch.setattr(repo, "get_child_tasks_by_parent_id", self_as_child)

    tree = await repo.build_task_tree(task)  # must terminate, not RecursionError
    assert tree.task is task
