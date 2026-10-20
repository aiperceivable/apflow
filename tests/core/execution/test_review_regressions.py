"""Regression coverage for execution selection, retries, hooks, and task cloning."""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from apflow.core.base import BaseTask
from apflow.core.execution.result_protocol import normalize_executor_result
from apflow.core.execution.task_creator import TaskCreator
from apflow.core.execution.task_executor import TaskExecutor
from apflow.core.execution.task_manager import TaskManager
from apflow.core.extensions import get_registry
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.durability.checkpoint import CheckpointManager
from apflow.durability.retry import RetryManager


@pytest.mark.asyncio
@pytest.mark.parametrize("short_circuit", [False, True])
async def test_synchronous_executor_hooks_run(
    sync_db_session: Session, short_circuit: bool
) -> None:
    calls: list[str] = []

    class HookProbe(BaseTask):
        id = "review_sync_hook_probe"
        name = "Synchronous hook probe"
        description = "Record executor-specific lifecycle hooks."
        _executor_hooks: dict[str, Any] = {}

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            calls.append("execute")
            return {"source": "execute"}

    def pre_hook(executor: BaseTask, task: Any, inputs: dict[str, Any]) -> Any:
        calls.append("pre")
        return {"source": "cache"} if short_circuit else None

    def post_hook(executor: BaseTask, task: Any, inputs: dict[str, Any], result: Any) -> None:
        calls.append("post")

    HookProbe._executor_hooks = {"pre_hook": pre_hook, "post_hook": post_hook}
    registry = get_registry()
    registry.register(HookProbe(), executor_class=HookProbe)
    try:
        manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
        task = await manager.task_repository.create_task(
            name="Hooks", params={"executor_id": HookProbe.id}
        )
        result = await manager._execute_task_with_schemas(task, {})
        assert calls == (["pre", "post"] if short_circuit else ["pre", "execute", "post"])
        assert result["result"] == {"source": "cache" if short_circuit else "execute"}
    finally:
        registry.unregister(HookProbe.id)


def test_legacy_usage_derives_total_without_changing_business_result() -> None:
    raw = {"token_usage": {"input": 80, "output": 20}, "answer": "done"}
    result = normalize_executor_result(raw, "legacy")
    assert "token_usage" in result
    assert result["token_usage"] == {"input": 80, "output": 20, "total": 100}
    assert result["result"] == raw
    assert raw["token_usage"] == {"input": 80, "output": 20}


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpointing", [False, True])
async def test_task_manager_retries_with_latest_checkpoint(
    sync_db_session: Session, checkpointing: bool
) -> None:
    observed: list[tuple[str, int]] = []
    attempts = 0

    class ResumeProbe(BaseTask):
        id = "review_retry_resume_probe"
        name = "Retry checkpoint probe"
        description = "Fail after the first step and resume the next attempt."

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.step = 0

        def supports_checkpoint(self) -> bool:
            return checkpointing

        def get_checkpoint(self) -> dict[str, Any] | None:
            return {"step": self.step} if checkpointing else None

        async def resume_from_checkpoint(self, checkpoint: dict[str, Any]) -> None:
            self.step = checkpoint["step"]
            observed.append(("resume", self.step))

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            observed.append(("execute", self.step))
            self.step += 1
            if (checkpointing and self.step == 1) or (not checkpointing and attempts == 1):
                raise RuntimeError("Connection failed after the first step")
            return {"step": self.step}

    registry = get_registry()
    registry.register(ResumeProbe(), executor_class=ResumeProbe)
    checkpoint_manager = CheckpointManager(sync_db_session)
    manager = TaskManager(
        sync_db_session,
        pre_hooks=[],
        post_hooks=[],
        checkpoint_manager=checkpoint_manager,
        retry_manager=RetryManager(),
    )
    try:
        task = await manager.task_repository.create_task(
            name="Retry probe",
            params={"executor_id": ResumeProbe.id},
            inputs={},
            max_attempts=2,
            backoff_base_seconds=0.1,
        )
        task_id = task.id
        await manager._execute_single_task(task, use_callback=False)
        sync_db_session.expire_all()
        completed = await manager.task_repository.get_task_by_id(task_id)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.attempt_count == 1
        assert observed == (
            [("execute", 0), ("resume", 1), ("execute", 1)]
            if checkpointing
            else [("execute", 0), ("execute", 0)]
        )
        assert completed.result == {"step": 2 if checkpointing else 1}
        assert await checkpoint_manager.load_checkpoint(task_id) is None
        assert task_id not in manager._executor_instances
    finally:
        registry.unregister(ResumeProbe.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["copy", "link", "mixed", "mixed_link"])
async def test_nonrecursive_clone_starts_independent_tree(
    sync_db_session: Session, mode: str
) -> None:
    repository = TaskRepository(sync_db_session)
    creator = TaskCreator(sync_db_session)
    root = await repository.create_task(name="Source root", status="completed")
    source = await repository.create_task(
        name="Source child", parent_id=root.id, task_tree_id=root.id, status="completed"
    )
    kwargs: dict[str, Any] = {"_original_task": source, "_recursive": False}
    if mode == "mixed_link":
        kwargs["_link_task_ids"] = [source.id]
    clone = await getattr(creator, "from_" + mode.removesuffix("_link"))(**kwargs)
    assert clone.task.task_tree_id == clone.task.id
    assert clone.task.parent_id is None
    loaded = await repository.build_task_tree(clone.task)
    assert loaded.task.id == clone.task.id
    assert {task.id for task in await repository.get_all_tasks_in_tree(clone.task)} == {
        clone.task.id
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_strict_subtree_validation_accepts_string_dependencies(
    sync_db_session: Session, external: bool
) -> None:
    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="Root")
    subtree = await repository.create_task(name="Subtree", parent_id=root.id)
    dependency = await repository.create_task(
        name="Dependency", parent_id=root.id if external else subtree.id
    )
    await repository.create_task(
        name="Consumer", parent_id=subtree.id, dependencies=[dependency.id]
    )
    creator = TaskCreator(sync_db_session)
    if external:
        with pytest.raises(ValueError, match="outside the subtree"):
            await creator.from_copy(subtree, _auto_include_deps=False)
    else:
        result = await creator.from_copy(subtree, _auto_include_deps=False)
        assert len(result.children) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("root_status", ["pending", "completed"])
async def test_child_execution_excludes_ancestors_and_unrelated_siblings(
    sync_db_session: Session, root_status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class SelectionProbe(BaseTask):
        id = "review_child_selection_probe"
        name = "Child selection probe"
        description = "Record the selected execution tasks."

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            calls.append(inputs["label"])
            return {"label": inputs["label"]}

    registry = get_registry()
    registry.register(SelectionProbe(), executor_class=SelectionProbe)
    try:
        repository = TaskRepository(sync_db_session)
        params = {"executor_id": SelectionProbe.id}
        root = await repository.create_task(
            name="Root", status=root_status, params=params, inputs={"label": "root"}
        )
        dependency = await repository.create_task(
            name="Dependency", parent_id=root.id, params=params, inputs={"label": "dependency"}
        )
        child = await repository.create_task(
            name="Child",
            parent_id=root.id,
            params=params,
            inputs={"label": "child"},
            dependencies=[dependency.id],
        )
        sibling = await repository.create_task(
            name="Unrelated", parent_id=root.id, params=params, inputs={"label": "unrelated"}
        )
        root_id, child_id, sibling_id = root.id, child.id, sibling.id
        executor = TaskExecutor()
        executor.refresh_config()
        monkeypatch.setattr(executor, "pre_hooks", [])
        monkeypatch.setattr(executor, "post_hooks", [])
        monkeypatch.setattr(executor, "_build_execution_components", lambda session: {})
        result = await executor.execute_task_by_id(child_id, db_session=sync_db_session)
        assert calls == ["dependency", "child"]
        assert result["status"] == "completed"
        assert result["result"] == {"label": "child"}
        persisted_root = await repository.get_task_by_id(root_id)
        persisted_sibling = await repository.get_task_by_id(sibling_id)
        assert persisted_root is not None and persisted_sibling is not None
        assert persisted_root.status == root_status
        assert persisted_sibling.status == "pending"
    finally:
        registry.unregister(SelectionProbe.id)


@pytest.mark.asyncio
async def test_task_creator_returns_all_forest_roots_with_unique_tree_ids(
    sync_db_session: Session,
) -> None:
    trees = await TaskCreator(sync_db_session).create_task_trees_from_array(
        [
            {"id": "forest_root_a", "name": "Root A"},
            {"id": "forest_child_a", "name": "Child A", "parent_id": "forest_root_a"},
            {"id": "forest_root_b", "name": "Root B"},
            {"id": "forest_child_b", "name": "Child B", "parent_id": "forest_root_b"},
        ]
    )
    assert [tree.task.id for tree in trees] == ["forest_root_a", "forest_root_b"]
    assert [tree.children[0].task.id for tree in trees] == ["forest_child_a", "forest_child_b"]
    assert len({tree.task.task_tree_id for tree in trees}) == 2
    for tree in trees:
        assert tree.task.task_tree_id == tree.task.id
        assert tree.children[0].task.task_tree_id == tree.task.id


@pytest.mark.asyncio
async def test_task_creator_rejects_multi_root_dependency_cycle_before_persisting(
    sync_db_session: Session,
) -> None:
    creator = TaskCreator(sync_db_session)
    with pytest.raises(ValueError, match="[Cc]ircular"):
        await creator.create_task_trees_from_array(
            [
                {"id": "cycle_a", "name": "A", "dependencies": ["cycle_b"]},
                {"id": "cycle_b", "name": "B", "dependencies": ["cycle_a"]},
            ]
        )
    assert await creator.task_repository.get_task_by_id("cycle_a") is None
    assert await creator.task_repository.get_task_by_id("cycle_b") is None


@pytest.mark.asyncio
async def test_task_creator_rejects_shared_tree_id_between_roots(
    sync_db_session: Session,
) -> None:
    creator = TaskCreator(sync_db_session)
    with pytest.raises(ValueError, match="cannot share the same task_tree_id"):
        await creator.create_task_trees_from_array(
            [
                {"name": "A", "task_tree_id": "shared_tree"},
                {"name": "B", "task_tree_id": "shared_tree"},
            ]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["already_running", "rejected"])
async def test_child_execution_preserves_rejection_status(
    sync_db_session: Session, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="Root")
    child = await repository.create_task(name="Child", parent_id=root.id)
    executor = TaskExecutor()
    monkeypatch.setattr(
        executor,
        "execute_task_tree",
        AsyncMock(return_value={"status": status, "root_task_id": root.id}),
    )
    result = await executor.execute_task_by_id(child.id, db_session=sync_db_session)
    assert result["status"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_child_execution_completion_hooks_and_stream_ignore_pending_parent(
    sync_db_session: Session, monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    from apflow.core.execution.streaming_callbacks import StreamingCallbacks

    class ChildProbe(BaseTask):
        id = "review_child_completion_probe"
        name = "Child completion probe"
        description = "Return a selected child business result."

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            return {"label": inputs["label"]}

    registry = get_registry()
    registry.register(ChildProbe(), executor_class=ChildProbe)
    events: list[dict[str, Any]] = []

    def capture_update(
        self: Any, update_type: str, task_id: str, status: str, **kwargs: Any
    ) -> None:
        events.append({"type": update_type, "task_id": task_id, "status": status, **kwargs})

    hook = AsyncMock()
    monkeypatch.setattr(TaskManager, "_call_task_tree_hooks", hook)
    monkeypatch.setattr(StreamingCallbacks, "send_update", capture_update)
    try:
        repository = TaskRepository(sync_db_session)
        root = await repository.create_task(
            name="Pending parent", params={"executor_id": ChildProbe.id}, inputs={"label": "parent"}
        )
        child = await repository.create_task(
            name="Selected child",
            parent_id=root.id,
            params={"executor_id": ChildProbe.id},
            inputs={"label": "child"},
        )
        root_id, child_id = root.id, child.id
        executor = TaskExecutor()
        executor.refresh_config()
        monkeypatch.setattr(executor, "pre_hooks", [])
        monkeypatch.setattr(executor, "post_hooks", [])
        monkeypatch.setattr(executor, "_build_execution_components", lambda session: {})
        result = await executor.execute_task_by_id(
            child_id,
            db_session=sync_db_session,
            use_streaming=streaming,
            streaming_callbacks_context=object() if streaming else None,
        )
        assert result["status"] == "completed"
        hook_types = [call.args[0] for call in hook.await_args_list]
        assert "on_tree_completed" in hook_types
        assert "on_tree_failed" not in hook_types
        if streaming:
            final_events = [event for event in events if event["type"] == "final"]
            assert len(final_events) == 1
            assert final_events[0]["status"] == "completed"
            assert final_events[0]["result"] == {"label": "child"}
            assert final_events[0]["progress"] == 1.0
        persisted_root = await repository.get_task_by_id(root_id)
        assert persisted_root is not None
        assert persisted_root.status == "pending"
    finally:
        registry.unregister(ChildProbe.id)
