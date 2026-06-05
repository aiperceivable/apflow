"""Tests for task management modules"""

import pytest
from unittest.mock import MagicMock, AsyncMock

from apflow.bridge.task_modules import (
    TaskCancelModule,
    TaskCopyModule,
    TaskCreateModule,
    TaskCreateTreeModule,
    TaskDeleteModule,
    TaskExecuteModule,
    TaskGetModule,
    TaskListModule,
    TaskRunningListModule,
    TaskScheduledListModule,
    TaskUpdateModule,
)


def _mock_task(task_id="abc-123", name="test_task", status="pending"):
    task = MagicMock()
    task.id = task_id
    task.name = name
    task.status = status
    task.created_at = MagicMock(isoformat=MagicMock(return_value="2026-01-01T00:00:00"))
    task.to_dict.return_value = {"id": task_id, "name": name, "status": status}
    return task


def _mock_repo(tasks=None, task=None):
    """Create a mock async repo with correct method names."""
    repo = MagicMock()
    repo.query_tasks = AsyncMock(return_value=tasks or [])
    repo.get_task_by_id = AsyncMock(return_value=task)
    repo.delete_task = AsyncMock(return_value=True)
    return repo


class TestTaskCreateModule:
    @pytest.mark.asyncio
    async def test_create_valid(self):
        creator = MagicMock()
        creator.create_task_trees_from_array = AsyncMock(return_value=[_mock_task()])
        repo = _mock_repo()

        module = TaskCreateModule(creator, repo)
        result = await module.execute({"name": "test_task"})
        assert result["id"] == "abc-123"
        assert result["name"] == "test_task"
        assert result["status"] == "pending"

    @pytest.mark.asyncio
    async def test_create_empty_name_raises(self):
        module = TaskCreateModule(MagicMock(), MagicMock())
        with pytest.raises(ValueError, match="non-empty"):
            await module.execute({"name": ""})

    @pytest.mark.asyncio
    async def test_create_missing_name_raises(self):
        module = TaskCreateModule(MagicMock(), MagicMock())
        with pytest.raises(ValueError, match="non-empty"):
            await module.execute({})


class TestTaskExecuteModule:
    @pytest.mark.asyncio
    async def test_execute_valid(self):
        manager = MagicMock()
        manager.execute_task = AsyncMock(return_value={"task_id": "abc", "status": "completed"})

        module = TaskExecuteModule(manager)
        result = await module.execute({"task_id": "abc"})
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_execute_empty_id_raises(self):
        module = TaskExecuteModule(MagicMock())
        with pytest.raises(ValueError):
            await module.execute({"task_id": ""})


class TestTaskListModule:
    @pytest.mark.asyncio
    async def test_list_defaults(self):
        repo = _mock_repo(tasks=[_mock_task()])

        module = TaskListModule(repo)
        result = await module.execute({})
        assert "tasks" in result
        assert "total" in result
        assert result["total"] == 1
        repo.query_tasks.assert_called_once_with(user_id=None, status=None, limit=50, offset=0)

    @pytest.mark.asyncio
    async def test_list_clamps_limit(self):
        repo = _mock_repo()
        module = TaskListModule(repo)

        await module.execute({"limit": 0})
        repo.query_tasks.assert_called_with(user_id=None, status=None, limit=1, offset=0)

        await module.execute({"limit": 5000})
        repo.query_tasks.assert_called_with(user_id=None, status=None, limit=1000, offset=0)


class TestTaskGetModule:
    @pytest.mark.asyncio
    async def test_get_valid(self):
        repo = _mock_repo(task=_mock_task())

        module = TaskGetModule(repo)
        result = await module.execute({"task_id": "abc-123"})
        assert result["id"] == "abc-123"

    @pytest.mark.asyncio
    async def test_get_not_found(self):
        repo = _mock_repo(task=None)

        module = TaskGetModule(repo)
        with pytest.raises(KeyError, match="not found"):
            await module.execute({"task_id": "nonexistent"})

    @pytest.mark.asyncio
    async def test_get_empty_id_raises(self):
        module = TaskGetModule(MagicMock())
        with pytest.raises(ValueError):
            await module.execute({"task_id": ""})


class TestTaskDeleteModule:
    @pytest.mark.asyncio
    async def test_delete_valid(self):
        repo = _mock_repo(task=_mock_task())

        module = TaskDeleteModule(repo)
        result = await module.execute({"task_id": "abc-123"})
        assert result["deleted"] is True
        repo.delete_task.assert_called_once_with("abc-123")

    @pytest.mark.asyncio
    async def test_delete_not_found(self):
        repo = _mock_repo(task=None)

        module = TaskDeleteModule(repo)
        with pytest.raises(KeyError, match="not found"):
            await module.execute({"task_id": "nonexistent"})


class TestTaskListInputValidation:
    """Regression: external (MCP/A2A) inputs are not schema-validated by default
    (apcore-mcp validate_inputs=False), so malformed limit/offset must not crash
    with a TypeError. (Review C1)
    """

    @pytest.mark.asyncio
    async def test_list_non_numeric_limit_raises_valueerror_not_typeerror(self):
        module = TaskListModule(_mock_repo())
        with pytest.raises(ValueError, match="integer"):
            await module.execute({"limit": "abc"})

    @pytest.mark.asyncio
    async def test_list_null_limit_falls_back_to_default(self):
        repo = _mock_repo()
        module = TaskListModule(repo)
        await module.execute({"limit": None, "offset": None})
        # Defaults applied (limit=50, offset=0) — no TypeError from min()/max().
        _, kwargs = repo.query_tasks.call_args
        assert kwargs["limit"] == 50
        assert kwargs["offset"] == 0

    @pytest.mark.asyncio
    async def test_list_limit_clamped(self):
        repo = _mock_repo()
        module = TaskListModule(repo)
        await module.execute({"limit": 99999})
        _, kwargs = repo.query_tasks.call_args
        assert kwargs["limit"] == 1000


class TestTaskCreateTreeInputValidation:
    """Regression: a non-object element in the tasks[] array must raise a clean
    ValueError, not an AttributeError from t.get(). (Review C2)
    """

    @pytest.mark.asyncio
    async def test_create_tree_non_dict_element_raises_valueerror(self):
        module = TaskCreateTreeModule(MagicMock(), MagicMock())
        with pytest.raises(ValueError, match="object"):
            await module.execute({"tasks": ["build", "test"]})

    @pytest.mark.asyncio
    async def test_create_tree_non_list_raises_valueerror(self):
        module = TaskCreateTreeModule(MagicMock(), MagicMock())
        with pytest.raises(ValueError, match="array"):
            await module.execute({"tasks": "not-a-list"})


class TestTaskUpdateFieldWhitelist:
    """Regression: only schema-advertised fields may be written; an arbitrary key
    (e.g. user_id) from an external agent must NOT reach update_task. (Review W1)
    """

    @pytest.mark.asyncio
    async def test_update_ignores_unlisted_fields(self):
        repo = _mock_repo(task=_mock_task())
        repo.update_task = AsyncMock(return_value=None)

        module = TaskUpdateModule(repo)
        await module.execute({"task_id": "t1", "name": "new", "user_id": "attacker"})

        _, kwargs = repo.update_task.call_args
        assert kwargs["name"] == "new"
        assert "user_id" not in kwargs

    @pytest.mark.asyncio
    async def test_update_does_not_mutate_caller_inputs(self):
        repo = _mock_repo(task=_mock_task())
        repo.update_task = AsyncMock(return_value=None)

        module = TaskUpdateModule(repo)
        inputs = {"task_id": "t1", "name": "new"}
        await module.execute(inputs)
        # task_id must remain in the caller's dict (no .pop mutation).
        assert inputs["task_id"] == "t1"


class TestTaskCancelModuleGuards:
    @pytest.mark.asyncio
    async def test_string_task_ids_raises(self):
        """A non-list task_ids (e.g. a bare string) must raise, not iterate char-by-char."""
        module = TaskCancelModule(MagicMock())
        with pytest.raises(ValueError, match="array"):
            await module.execute({"task_ids": "abc123"})

    @pytest.mark.asyncio
    async def test_empty_task_ids_raises(self):
        module = TaskCancelModule(MagicMock())
        with pytest.raises(ValueError, match="non-empty"):
            await module.execute({"task_ids": []})


class TestTaskReuseOverrideAllowlist:
    @pytest.mark.asyncio
    async def test_copy_strips_disallowed_override_fields(self):
        """Only content fields pass through; tenancy/structural keys are dropped."""
        tree = MagicMock()
        tree.task.id = "new-root"
        tree.to_list.return_value = [tree.task]
        creator = MagicMock()
        creator.from_copy = AsyncMock(return_value=tree)
        repo = _mock_repo(task=_mock_task())

        module = TaskCopyModule(creator, repo)
        await module.execute(
            {
                "task_id": "t1",
                "overrides": {
                    "name": "variant",
                    "user_id": "attacker",
                    "task_tree_id": "evil-tree",
                    "origin_type": "link",
                },
            }
        )

        _, kwargs = creator.from_copy.call_args
        assert kwargs["name"] == "variant"
        assert "user_id" not in kwargs
        assert "task_tree_id" not in kwargs
        assert "origin_type" not in kwargs

    @pytest.mark.asyncio
    async def test_non_dict_overrides_raises(self):
        creator = MagicMock()
        repo = _mock_repo(task=_mock_task())
        module = TaskCopyModule(creator, repo)
        with pytest.raises(ValueError, match="overrides must be an object"):
            await module.execute({"task_id": "t1", "overrides": "nope"})


class TestTaskRunningListModuleClamp:
    @pytest.mark.asyncio
    async def test_clamps_limit_to_schema_maximum(self):
        repo = _mock_repo(tasks=[])
        module = TaskRunningListModule(repo)
        await module.execute({"limit": 5000})
        _, kwargs = repo.query_tasks.call_args
        assert kwargs["limit"] == 100


class TestTaskScheduledListModule:
    @pytest.mark.asyncio
    async def test_uses_db_filter_with_user_id_scoping(self):
        """Scheduled listing filters + scopes by user_id at the DB layer."""
        repo = MagicMock()
        repo.get_scheduled_tasks = AsyncMock(return_value=[])
        repo.query_tasks = AsyncMock(return_value=[])

        module = TaskScheduledListModule(repo)
        await module.execute({"user_id": "u1", "limit": 5, "enabled_only": True})

        repo.get_scheduled_tasks.assert_called_once_with(enabled_only=True, user_id="u1", limit=5)
        repo.query_tasks.assert_not_called()
