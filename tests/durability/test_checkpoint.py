"""Tests for checkpoint module"""

import base64

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apflow.core.storage.sqlalchemy.models import Base
from apflow.durability.checkpoint import CheckpointManager


@pytest.fixture
def db_session():
    """Create a fresh in-memory SQLite session with all tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def checkpoint_mgr(db_session):
    return CheckpointManager(db_session)


@pytest.fixture
def task_id(db_session):
    """Create a task and return its ID."""
    from apflow.core.storage.sqlalchemy.models import TaskModel

    task = TaskModel.create({"name": "test_task"})
    db_session.add(task)
    db_session.commit()
    return task.id


class TestSaveCheckpoint:
    @pytest.mark.asyncio
    async def test_save_returns_uuid(self, checkpoint_mgr, task_id):
        cp_id = await checkpoint_mgr.save_checkpoint(task_id, {"step": 1, "data": "hello"})
        assert isinstance(cp_id, str)
        assert len(cp_id) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_save_empty_task_id_raises(self, checkpoint_mgr):
        with pytest.raises(ValueError, match="non-empty"):
            await checkpoint_mgr.save_checkpoint("", {"step": 1})

    @pytest.mark.asyncio
    async def test_save_non_dict_raises(self, checkpoint_mgr, task_id):
        with pytest.raises(TypeError, match="must be a dict"):
            await checkpoint_mgr.save_checkpoint(task_id, "not a dict")

    @pytest.mark.asyncio
    async def test_save_non_serializable_raises(self, checkpoint_mgr, task_id):
        with pytest.raises(ValueError, match="not JSON-serializable"):
            await checkpoint_mgr.save_checkpoint(task_id, {"fn": lambda: None})

    @pytest.mark.asyncio
    async def test_save_with_step_name(self, checkpoint_mgr, task_id):
        cp_id = await checkpoint_mgr.save_checkpoint(task_id, {"data": "x"}, step_name="phase_2")
        assert cp_id is not None

    @pytest.mark.asyncio
    async def test_save_base64_binary(self, checkpoint_mgr, task_id):
        data = {"binary": base64.b64encode(b"hello world").decode()}
        await checkpoint_mgr.save_checkpoint(task_id, data)
        loaded = await checkpoint_mgr.load_checkpoint(task_id)
        assert loaded["binary"] == base64.b64encode(b"hello world").decode()


class TestLoadCheckpoint:
    @pytest.mark.asyncio
    async def test_load_returns_latest(self, checkpoint_mgr, task_id):
        await checkpoint_mgr.save_checkpoint(task_id, {"step": 1})
        await checkpoint_mgr.save_checkpoint(task_id, {"step": 2})
        loaded = await checkpoint_mgr.load_checkpoint(task_id)
        assert loaded == {"step": 2}

    @pytest.mark.asyncio
    async def test_load_none_when_empty(self, checkpoint_mgr, task_id):
        loaded = await checkpoint_mgr.load_checkpoint(task_id)
        assert loaded is None

    @pytest.mark.asyncio
    async def test_load_empty_task_id_raises(self, checkpoint_mgr):
        with pytest.raises(ValueError, match="non-empty"):
            await checkpoint_mgr.load_checkpoint("")


class TestDeleteCheckpoints:
    @pytest.mark.asyncio
    async def test_delete_returns_count(self, checkpoint_mgr, task_id):
        await checkpoint_mgr.save_checkpoint(task_id, {"step": 1})
        await checkpoint_mgr.save_checkpoint(task_id, {"step": 2})
        await checkpoint_mgr.save_checkpoint(task_id, {"step": 3})
        count = await checkpoint_mgr.delete_checkpoints(task_id)
        assert count == 3
        loaded = await checkpoint_mgr.load_checkpoint(task_id)
        assert loaded is None

    @pytest.mark.asyncio
    async def test_delete_empty_task_id_raises(self, checkpoint_mgr):
        with pytest.raises(ValueError, match="non-empty"):
            await checkpoint_mgr.delete_checkpoints("")

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_zero(self, checkpoint_mgr):
        count = await checkpoint_mgr.delete_checkpoints("nonexistent-id")
        assert count == 0


class TestCheckpointManagerInit:
    def test_none_db_raises(self):
        with pytest.raises(TypeError, match="must not be None"):
            CheckpointManager(None)
