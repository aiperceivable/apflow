"""Regression coverage for task-tree completion and dependency payloads."""

import asyncio
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from apflow.core.base import BaseTask
from apflow.core.execution.task_creator import TaskCreator
from apflow.core.execution.task_executor import TaskExecutor
from apflow.core.execution.task_manager import TaskManager
from apflow.core.extensions import get_registry
from apflow.core.storage.sqlalchemy.models import TaskModel
from apflow.core.types import TaskTreeNode


class PayloadExecutor(BaseTask):
    id = "result_test_payload"
    name = "Payload"
    description = "Return the supplied test payload."

    def get_output_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"value": {"type": "integer"}}}

    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return inputs["payload"]


class TypedReceiverExecutor(BaseTask):
    id = "result_test_receiver"
    name = "Receiver"
    description = "Return resolved test inputs."

    def get_input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"value": {"type": "integer"}}}

    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"received": inputs}


class FailedRootExecutor(BaseTask):
    id = "result_test_failed_root"
    name = "Failed root"
    description = "Fail after dependencies complete."

    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"error": "Root aggregation failed"}


@pytest.fixture(autouse=True)
def result_executors() -> Iterator[None]:
    registry = get_registry()
    classes = (PayloadExecutor, TypedReceiverExecutor, FailedRootExecutor)
    for executor_class in classes:
        registry.register(executor_class(), executor_class=executor_class, override=True)
    yield
    for executor_class in classes:
        registry.unregister(executor_class.id)


def dependency_tasks(
    session: Session,
    payloads: list[Any],
    inputs: dict[str, Any] | None = None,
    dependency_form: str = "object",
) -> tuple[TaskManager, TaskModel, list[TaskModel]]:
    root = TaskModel(id="root", name="Root", inputs=inputs or {}, status="pending")
    sources = [
        TaskModel(
            id=f"source-{index}",
            name=f"Source {index}",
            parent_id="root",
            status="completed",
            result=payload,
        )
        for index, payload in enumerate(payloads)
    ]
    root.update_from_dict(
        {
            "dependencies": [
                str(source.id) if dependency_form == "string" else {"id": str(source.id)}
                for source in sources
            ]
        }
    )
    session.add_all([root, *sources])
    session.commit()
    return TaskManager(session, pre_hooks=[], post_hooks=[]), root, sources


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency_form", ["object", "string"])
@pytest.mark.parametrize(
    "payload",
    [
        {"result": {"answer": 42}, "evidence": ["document"]},
        {},
        [],
        0,
        False,
        "",
    ],
)
async def test_dependency_result_preserves_full_payload(
    sync_db_session: Session,
    dependency_form: str,
    payload: Any,
) -> None:
    manager, root, sources = dependency_tasks(
        sync_db_session,
        [payload],
        {"existing": "value"},
        dependency_form,
    )
    resolved = await manager.resolve_task_dependencies(root)
    assert resolved == {"existing": "value", str(sources[0].id): payload}
    assert cast(dict[str, Any], root.inputs) == {"existing": "value"}


@pytest.mark.asyncio
async def test_dependency_results_preserve_colliding_fields(sync_db_session: Session) -> None:
    manager, root, sources = dependency_tasks(
        sync_db_session,
        [{"price": 10}, {"price": 20}],
        {"price": 99},
    )
    resolved = await manager.resolve_task_dependencies(root)
    assert resolved == {
        "price": 99,
        str(sources[0].id): {"price": 10},
        str(sources[1].id): {"price": 20},
    }


@pytest.mark.asyncio
async def test_dependency_result_does_not_share_mutable_payload(sync_db_session: Session) -> None:
    manager, root, sources = dependency_tasks(sync_db_session, [{"items": [1]}])
    resolved = await manager.resolve_task_dependencies(root)
    resolved[str(sources[0].id)]["items"].append(2)
    assert cast(dict[str, Any], sources[0].result) == {"items": [1]}


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["params", "schemas", "both"])
async def test_dependency_namespace_is_independent_of_executor_schema(
    sync_db_session: Session,
    location: str,
) -> None:
    manager, root, sources = dependency_tasks(sync_db_session, [{"value": 7}])
    for task, executor_id in [(root, TypedReceiverExecutor.id), (sources[0], PayloadExecutor.id)]:
        if location in ("params", "both"):
            task.update_from_dict({"params": {"executor_id": executor_id}})
        if location in ("schemas", "both"):
            task.update_from_dict(
                {"schemas": {"method": executor_id if location == "schemas" else "unknown"}}
            )
    sync_db_session.commit()
    assert await manager.resolve_task_dependencies(root) == {str(sources[0].id): {"value": 7}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inputs,payloads",
    [
        ({"value": 99}, [{"value": 10}]),
        ({}, [{"value": 10}, {"value": 20}]),
    ],
)
async def test_dependency_namespace_preserves_explicit_and_multiple_values(
    sync_db_session: Session,
    inputs: dict[str, Any],
    payloads: list[dict[str, Any]],
) -> None:
    manager, root, sources = dependency_tasks(sync_db_session, payloads, inputs)
    root.update_from_dict({"params": {"executor_id": TypedReceiverExecutor.id}})
    for source in sources:
        source.update_from_dict({"params": {"executor_id": PayloadExecutor.id}})
    sync_db_session.commit()
    resolved = await manager.resolve_task_dependencies(root)
    assert resolved == {
        **inputs,
        **{str(source.id): payload for source, payload in zip(sources, payloads)},
    }
    assert cast(dict[str, Any], root.inputs) == inputs


@pytest.mark.parametrize("status", ["pending", "in_progress", "failed", "cancelled", "completed"])
def test_tree_status_includes_root(status: str) -> None:
    root = TaskTreeNode(TaskModel(name="Root", status=status, progress=0))
    root.add_child(TaskTreeNode(TaskModel(name="Leaf", status="completed", progress=1)))
    assert root.calculate_status() == status


def test_tree_progress_includes_intermediate_tasks() -> None:
    root = TaskTreeNode(TaskModel(name="Root", status="pending", progress=0))
    branch = TaskTreeNode(TaskModel(name="Branch", status="failed", progress=0))
    root.add_child(branch)
    branch.add_child(TaskTreeNode(TaskModel(name="Leaf", status="completed", progress=1)))
    root.add_child(TaskTreeNode(TaskModel(name="Other leaf", status="completed", progress=1)))
    assert root.calculate_progress() == 0.5
    assert root.calculate_status() == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_failed_root_never_emits_successful_tree_completion(
    sync_db_session: Session,
    streaming: bool,
) -> None:
    creator = TaskCreator(sync_db_session)
    tree = await creator.create_task_tree_from_array(
        [
            {
                "id": "root",
                "name": "Root",
                "params": {"executor_id": FailedRootExecutor.id},
                "dependencies": [{"id": "leaf"}],
            },
            {
                "id": "leaf",
                "name": "Leaf",
                "parent_id": "root",
                "params": {"executor_id": PayloadExecutor.id},
                "inputs": {"payload": {"value": 1}},
            },
        ]
    )
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    manager.streaming_callbacks.set_streaming_context(events, None)
    with patch.object(manager, "_call_task_tree_hooks", new_callable=AsyncMock) as hooks:
        if streaming:
            await manager.distribute_task_tree_with_streaming(tree)
            await asyncio.gather(*manager.streaming_callbacks._pending_tasks)
            updates = [events.get_nowait() for _ in range(events.qsize())]
            final = next(event for event in updates if event["type"] == "final")
            assert final["status"] == "failed"
        else:
            await manager.distribute_task_tree(tree)
        hook_types = [call.args[0] for call in hooks.call_args_list]
        assert "on_tree_completed" not in hook_types
        assert "on_tree_failed" in hook_types
    assert tree.calculate_status() == "failed"


@pytest.mark.asyncio
async def test_aggregate_results_keeps_nested_payload(sync_db_session: Session) -> None:
    payload = {"result": {"answer": 42}, "evidence": ["document"]}
    manager, root, sources = dependency_tasks(sync_db_session, [payload])
    root.update_from_dict({"params": {"executor_id": "aggregate_results_executor"}})
    sync_db_session.commit()
    tree = await manager.task_repository.build_task_tree(root)
    await manager.distribute_task_tree(tree)
    assert cast(dict[str, Any], root.result)["results"] == {str(sources[0].id): payload}


@pytest.mark.asyncio
async def test_execution_summary_includes_failed_child(sync_db_session: Session) -> None:
    tree = await TaskCreator(sync_db_session).create_task_tree_from_array(
        [
            {
                "id": "root",
                "name": "Root",
                "params": {"executor_id": "aggregate_results_executor"},
                "dependencies": [{"id": "failed", "required": False}, {"id": "leaf"}],
            },
            {
                "id": "failed",
                "name": "Failed",
                "parent_id": "root",
                "params": {"executor_id": FailedRootExecutor.id},
            },
            {
                "id": "leaf",
                "name": "Leaf",
                "parent_id": "root",
                "params": {"executor_id": PayloadExecutor.id},
                "inputs": {"payload": {"value": 1}},
            },
        ]
    )
    result = await TaskExecutor().execute_task_tree(
        tree,
        root_task_id=str(tree.task.id),
        db_session=sync_db_session,
    )
    assert str(tree.task.status) == "completed"
    assert result["status"] == "reconciliation_required"
    assert result["progress"] == pytest.approx(2 / 3)
    assert result["result"]["results"] == {"leaf": {"value": 1}}
