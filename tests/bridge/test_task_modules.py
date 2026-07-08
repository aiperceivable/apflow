"""Tests for task management modules"""

import asyncio

import pytest
from apcore.errors import ModuleError
from unittest.mock import AsyncMock, MagicMock, patch

from apflow.bridge.task_modules import (
    TaskArchiveModule,
    TaskCancelModule,
    TaskChildrenModule,
    TaskCloneMixedModule,
    TaskCopyModule,
    TaskCreateModule,
    TaskCreateTreeModule,
    TaskDeleteModule,
    TaskExecuteModule,
    TaskGetModule,
    TaskLinkModule,
    TaskListModule,
    TaskRunningListModule,
    TaskScheduledListModule,
    TaskTreeModule,
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
        with pytest.raises(ModuleError, match="non-empty"):
            await module.execute({"name": ""})

    @pytest.mark.asyncio
    async def test_create_missing_name_raises(self):
        module = TaskCreateModule(MagicMock(), MagicMock())
        with pytest.raises(ModuleError, match="non-empty"):
            await module.execute({})

    @pytest.mark.asyncio
    async def test_create_coerces_numeric_string_int_fields(self):
        """Regression: priority/token_budget/max_attempts from external MCP/A2A
        callers were passed straight through with no type coercion, unlike
        sibling limit/offset/max_runs fields (which already use _coerce_int).
        A numeric string must be coerced to int before reaching the creator.
        (Review CRITICAL #14)
        """
        creator = MagicMock()
        creator.create_task_trees_from_array = AsyncMock(return_value=[_mock_task()])
        repo = _mock_repo()

        module = TaskCreateModule(creator, repo)
        await module.execute(
            {"name": "t", "priority": "1", "token_budget": "500", "max_attempts": "5"}
        )

        (sent_tasks,), _ = creator.create_task_trees_from_array.call_args
        assert sent_tasks[0]["priority"] == 1
        assert sent_tasks[0]["token_budget"] == 500
        assert sent_tasks[0]["max_attempts"] == 5

    @pytest.mark.asyncio
    async def test_create_invalid_priority_raises_valueerror(self):
        module = TaskCreateModule(MagicMock(), MagicMock())
        with pytest.raises(ModuleError):
            await module.execute({"name": "t", "priority": "not-a-number"})


class _FakeTaskExecutor:
    """Stand-in for TaskExecutor that returns a canned result and (optionally)
    feeds progress events into the streaming callbacks queue."""

    def __init__(self, events=None, result=None):
        self._events = events or []
        self._result = (
            result if result is not None else {"status": "completed", "root_task_id": "abc"}
        )

    async def execute_task_by_id(
        self, task_id, use_streaming=False, streaming_callbacks_context=None, **_kw
    ):
        if use_streaming and streaming_callbacks_context is not None:
            for event in self._events:
                await streaming_callbacks_context.put(event)
        return self._result


class _DetachedTaskExecutor:
    """Mirrors the real engine: progress events are enqueued via DETACHED
    fire-and-forget tasks (like StreamingCallbacks), NOT inline — so the puts can
    land just after execute returns. Exercises the stream() drain race directly."""

    def __init__(self, events):
        self._events = events
        self._pending: set = set()

    async def execute_task_by_id(
        self, task_id, use_streaming=False, streaming_callbacks_context=None, **_kw
    ):
        if use_streaming and streaming_callbacks_context is not None:
            for event in self._events:
                t = asyncio.create_task(streaming_callbacks_context.put(event))
                self._pending.add(t)
                t.add_done_callback(self._pending.discard)
        return {"status": "completed", "root_task_id": task_id}


class TestTaskExecuteModule:
    @pytest.mark.asyncio
    async def test_execute_runs_via_task_executor(self):
        fake = _FakeTaskExecutor(result={"task_id": "abc", "status": "completed"})
        with patch("apflow.core.execution.task_executor.TaskExecutor", return_value=fake):
            module = TaskExecuteModule(MagicMock())
            result = await module.execute({"task_id": "abc"})
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_execute_empty_id_raises(self):
        module = TaskExecuteModule(MagicMock())
        with pytest.raises(ModuleError):
            await module.execute({"task_id": ""})

    @pytest.mark.asyncio
    async def test_stream_relays_progress_then_result(self):
        events = [
            {"type": "task_start", "task_id": "abc", "status": "in_progress"},
            {"type": "progress", "task_id": "abc", "progress": 0.5},
        ]
        fake = _FakeTaskExecutor(
            events=events, result={"status": "completed", "root_task_id": "abc"}
        )
        with patch("apflow.core.execution.task_executor.TaskExecutor", return_value=fake):
            module = TaskExecuteModule(MagicMock())
            collected = [event async for event in module.stream({"task_id": "abc"}, None)]

        types = [e.get("type") for e in collected]
        assert "task_start" in types
        assert "progress" in types
        assert collected[-1]["type"] == "result"
        assert collected[-1]["result"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_stream_empty_id_raises(self):
        module = TaskExecuteModule(MagicMock())
        with pytest.raises(ModuleError):
            async for _event in module.stream({"task_id": ""}, None):
                pass

    @pytest.mark.asyncio
    async def test_stream_delivers_events_from_detached_producer(self):
        # Regression for the drain race: when progress events are enqueued via
        # detached fire-and-forget tasks (the real StreamingCallbacks path), the
        # late terminal event must NOT be dropped. The old one-shot empty() drain
        # missed these; the grace-window drain delivers them.
        events = [
            {"type": "task_start", "task_id": "abc"},
            {"type": "progress", "task_id": "abc", "progress": 0.5},
            {"type": "task_completed", "task_id": "abc"},
        ]
        fake = _DetachedTaskExecutor(events)
        with patch("apflow.core.execution.task_executor.TaskExecutor", return_value=fake):
            module = TaskExecuteModule(MagicMock())
            collected = [event async for event in module.stream({"task_id": "abc"}, None)]

        types = [e.get("type") for e in collected]
        assert "task_start" in types
        assert "task_completed" in types  # the late detached put is not dropped
        assert collected[-1]["type"] == "result"

    @pytest.mark.asyncio
    async def test_stream_cancels_background_run_on_early_close(self):
        # Regression for the leak: closing the generator early (SSE client
        # disconnect) must cancel the background execution, not orphan it.
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class _OneEventThenHang:
            async def execute_task_by_id(
                self, task_id, use_streaming=False, streaming_callbacks_context=None, **_kw
            ):
                started.set()
                if streaming_callbacks_context is not None:
                    await streaming_callbacks_context.put({"type": "task_start"})
                try:
                    await asyncio.sleep(10)  # long-running execution
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return {"status": "completed"}

        with patch(
            "apflow.core.execution.task_executor.TaskExecutor",
            return_value=_OneEventThenHang(),
        ):
            gen = TaskExecuteModule(MagicMock()).stream({"task_id": "abc"}, None)
            first = await gen.__anext__()
            assert first["type"] == "task_start"
            await gen.aclose()  # type: ignore[attr-defined]  # disconnect -> finally cancels run

        assert started.is_set()
        assert cancelled.is_set()


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
        with pytest.raises(ModuleError, match="not found"):
            await module.execute({"task_id": "nonexistent"})

    @pytest.mark.asyncio
    async def test_get_empty_id_raises(self):
        module = TaskGetModule(MagicMock())
        with pytest.raises(ModuleError):
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
        with pytest.raises(ModuleError, match="not found"):
            await module.execute({"task_id": "nonexistent"})


class TestTaskListInputValidation:
    """Regression: external (MCP/A2A) inputs are not schema-validated by default
    (apcore-mcp validate_inputs=False), so malformed limit/offset must not crash
    with a TypeError. (Review C1)
    """

    @pytest.mark.asyncio
    async def test_list_non_numeric_limit_raises_valueerror_not_typeerror(self):
        module = TaskListModule(_mock_repo())
        with pytest.raises(ModuleError, match="integer"):
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
        with pytest.raises(ModuleError, match="object"):
            await module.execute({"tasks": ["build", "test"]})

    @pytest.mark.asyncio
    async def test_create_tree_non_list_raises_valueerror(self):
        module = TaskCreateTreeModule(MagicMock(), MagicMock())
        with pytest.raises(ModuleError, match="array"):
            await module.execute({"tasks": "not-a-list"})


class TestTaskCreateTreeFieldWhitelist:
    """Regression: per-task fields in the tasks[] array must be restricted to the
    schema-advertised allowlist. Without it, an external MCP/A2A agent can smuggle
    origin_type=link/original_task_id/user_id straight into build_task(), bypassing
    from_link's ownership and completion checks entirely. (Review #24)
    """

    @staticmethod
    def _mock_tree():
        tree = MagicMock()
        tree.task.id = "root-1"
        tree.children = []
        return tree

    @pytest.mark.asyncio
    async def test_disallowed_fields_stripped_before_create(self):
        creator = MagicMock()
        creator.create_task_tree_from_array = AsyncMock(return_value=self._mock_tree())

        module = TaskCreateTreeModule(creator, MagicMock())
        await module.execute(
            {
                "tasks": [
                    {
                        "name": "Task A",
                        "origin_type": "link",
                        "original_task_id": "victim-task",
                        "user_id": "attacker",
                    }
                ]
            }
        )

        (sent_tasks,), _ = creator.create_task_tree_from_array.call_args
        assert sent_tasks[0]["name"] == "Task A"
        assert "origin_type" not in sent_tasks[0]
        assert "original_task_id" not in sent_tasks[0]
        assert "user_id" not in sent_tasks[0]

    @pytest.mark.asyncio
    async def test_allowed_fields_pass_through(self):
        creator = MagicMock()
        creator.create_task_tree_from_array = AsyncMock(return_value=self._mock_tree())

        module = TaskCreateTreeModule(creator, MagicMock())
        await module.execute(
            {
                "tasks": [
                    {
                        "id": "task-a",
                        "name": "Task A",
                        "priority": 1,
                        "dependencies": [],
                    }
                ]
            }
        )

        (sent_tasks,), _ = creator.create_task_tree_from_array.call_args
        assert sent_tasks[0]["id"] == "task-a"
        assert sent_tasks[0]["priority"] == 1
        assert sent_tasks[0]["dependencies"] == []

    @pytest.mark.asyncio
    async def test_invalid_priority_in_task_item_raises_valueerror(self):
        """Regression: per-task priority/token_budget/max_attempts values were
        never validated/coerced, unlike sibling limit/offset/max_runs fields.
        (Review CRITICAL #14)
        """
        module = TaskCreateTreeModule(MagicMock(), MagicMock())
        with pytest.raises(ModuleError):
            await module.execute({"tasks": [{"name": "Task A", "priority": "not-a-number"}]})


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

    @pytest.mark.asyncio
    async def test_update_invalid_priority_raises_valueerror(self):
        """Regression: priority was never validated/coerced before reaching
        update_task, unlike sibling limit/offset/max_runs fields. (Review
        CRITICAL #14)
        """
        repo = _mock_repo(task=_mock_task())
        repo.update_task = AsyncMock(return_value=None)

        module = TaskUpdateModule(repo)
        with pytest.raises(ModuleError):
            await module.execute({"task_id": "t1", "priority": "not-a-number"})


class TestTaskCancelModuleGuards:
    @pytest.mark.asyncio
    async def test_string_task_ids_raises(self):
        """A non-list task_ids (e.g. a bare string) must raise, not iterate char-by-char."""
        module = TaskCancelModule(MagicMock())
        with pytest.raises(ModuleError, match="array"):
            await module.execute({"task_ids": "abc123"})

    @pytest.mark.asyncio
    async def test_empty_task_ids_raises(self):
        module = TaskCancelModule(MagicMock())
        with pytest.raises(ModuleError, match="non-empty"):
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
        with pytest.raises(ModuleError, match="overrides must be an object"):
            await module.execute({"task_id": "t1", "overrides": "nope"})

    @pytest.mark.asyncio
    async def test_invalid_priority_override_raises_valueerror(self):
        """Regression: overrides["priority"] was never validated/coerced
        before reaching the creator. (Review CRITICAL #14)
        """
        creator = MagicMock()
        repo = _mock_repo(task=_mock_task())
        module = TaskCopyModule(creator, repo)
        with pytest.raises(ModuleError):
            await module.execute({"task_id": "t1", "overrides": {"priority": "not-a-number"}})


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


class TestModuleAnnotations:
    """All task modules must carry correct apcore ModuleAnnotations."""

    def test_readonly_modules(self):
        for ModuleCls, dep in [
            (TaskListModule, (_mock_repo(),)),
            (TaskGetModule, (_mock_repo(),)),
            (TaskTreeModule, (_mock_repo(),)),
            (TaskChildrenModule, (_mock_repo(),)),
            (TaskRunningListModule, (_mock_repo(),)),
            (TaskScheduledListModule, (_mock_repo(),)),
        ]:
            m = ModuleCls(*dep)
            assert m.annotations.readonly is True, f"{ModuleCls.__name__} should be readonly"
            assert m.annotations.idempotent is True, f"{ModuleCls.__name__} should be idempotent"
            assert (
                m.annotations.destructive is False
            ), f"{ModuleCls.__name__} should not be destructive"

    def test_destructive_requires_approval_modules(self):
        for ModuleCls, dep in [
            (TaskExecuteModule, (MagicMock(),)),
            (TaskDeleteModule, (_mock_repo(),)),
            (TaskCancelModule, (MagicMock(),)),
        ]:
            m = ModuleCls(*dep)
            assert m.annotations.destructive is True, f"{ModuleCls.__name__} should be destructive"
            assert (
                m.annotations.requires_approval is True
            ), f"{ModuleCls.__name__} should require approval"
            assert m.annotations.readonly is False, f"{ModuleCls.__name__} should not be readonly"

    def test_idempotent_modules(self):
        for ModuleCls, dep in [
            (TaskLinkModule, (_mock_repo(), MagicMock())),
            (TaskArchiveModule, (_mock_repo(), MagicMock())),
        ]:
            m = ModuleCls(*dep)
            assert m.annotations.idempotent is True, f"{ModuleCls.__name__} should be idempotent"

    def test_non_destructive_mutating_modules(self):
        for ModuleCls, dep in [
            (TaskCreateModule, (MagicMock(), _mock_repo())),
            (TaskCopyModule, (MagicMock(), _mock_repo())),
            (TaskCloneMixedModule, (MagicMock(), _mock_repo())),
            (TaskUpdateModule, (_mock_repo(),)),
        ]:
            m = ModuleCls(*dep)
            assert (
                m.annotations.destructive is False
            ), f"{ModuleCls.__name__} should not be destructive"
            assert m.annotations.readonly is False, f"{ModuleCls.__name__} should not be readonly"


class TestPreviewMethods:
    """Destructive modules expose preview() for the __apcore_module_preview meta-tool."""

    @pytest.mark.asyncio
    async def test_execute_preview_with_task_id(self):
        m = TaskExecuteModule(MagicMock())
        result = await m.preview({"task_id": "t-99"})
        assert len(result.changes) == 1
        assert result.changes[0].action == "execute"
        assert "t-99" in result.changes[0].target
        assert "t-99" in result.changes[0].summary

    @pytest.mark.asyncio
    async def test_execute_preview_without_task_id(self):
        m = TaskExecuteModule(MagicMock())
        result = await m.preview({})
        assert len(result.changes) == 1
        assert "not provided" in result.changes[0].summary

    @pytest.mark.asyncio
    async def test_delete_preview_with_task_id(self):
        m = TaskDeleteModule(_mock_repo())
        result = await m.preview({"task_id": "t-42"})
        assert len(result.changes) == 1
        assert result.changes[0].action == "delete"
        assert "t-42" in result.changes[0].target
        assert "Permanently delete" in result.changes[0].summary

    @pytest.mark.asyncio
    async def test_delete_preview_without_task_id(self):
        m = TaskDeleteModule(_mock_repo())
        result = await m.preview({})
        assert len(result.changes) == 1
        assert "not provided" in result.changes[0].summary

    @pytest.mark.asyncio
    async def test_cancel_preview_multiple_ids(self):
        m = TaskCancelModule(MagicMock())
        result = await m.preview({"task_ids": ["t-1", "t-2", "t-3"]})
        assert len(result.changes) == 3
        targets = {c.target for c in result.changes}
        assert "task:t-1" in targets
        assert "task:t-3" in targets
        for c in result.changes:
            assert c.action == "cancel"

    @pytest.mark.asyncio
    async def test_cancel_preview_empty_ids(self):
        m = TaskCancelModule(MagicMock())
        result = await m.preview({})
        assert result.changes == []

    @pytest.mark.asyncio
    async def test_cancel_preview_none_ids(self):
        m = TaskCancelModule(MagicMock())
        result = await m.preview({"task_ids": None})
        assert result.changes == []
