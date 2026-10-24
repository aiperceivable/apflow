"""Integration regressions for references, immutable archives, and cancellation."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from apflow.core.execution.errors import ValidationError
from apflow.core.execution.task_creator import TaskCreator
from apflow.core.execution.task_executor import TaskExecutor
from apflow.core.execution.task_manager import TaskManager
from apflow.core.storage.sqlalchemy.models import TaskModel, TaskOriginType
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"items": [1]}, None, False, 0, []])
async def test_link_dependency_matches_query_without_shared_payload(
    sync_db_session: Session, payload: Any
) -> None:
    repo = TaskRepository(sync_db_session)
    source = await repo.create_task(name="Source", status="completed", result=payload, user_id="a")
    linked = await TaskCreator(sync_db_session).from_link(source, _recursive=False)
    consumer = await repo.create_task(name="Consumer", user_id="a", dependencies=[linked.task.id])
    await repo.update_task(linked.task.id, parent_id=consumer.id, task_tree_id=consumer.id)
    manager = TaskManager(sync_db_session)
    resolved = await manager.resolve_task_dependencies(consumer)
    api_tree = await repo.get_task_tree_for_api(consumer)
    assert resolved[linked.task.id] == api_tree.children[0].task.result == payload
    if isinstance(payload, dict):
        resolved[linked.task.id]["items"].append(2)
        assert source.result == {"items": [1]}
        api_tree.children[0].task.result["items"].append(3)
        assert source.result == {"items": [1]}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "cycle", "owner", "unfinished"])
async def test_effective_link_rejects_invalid_reference(
    sync_db_session: Session, failure: str
) -> None:
    repo = TaskRepository(sync_db_session)
    source = await repo.create_task(name="Source", status="completed", user_id="a")
    link = await repo.create_task(
        name="Link",
        status="completed",
        user_id="a",
        origin_type=TaskOriginType.link,
        original_task_id=source.id,
    )
    if failure == "missing":
        link.original_task_id = "missing"
    elif failure == "cycle":
        source.origin_type = TaskOriginType.link
        source.original_task_id = link.id
    elif failure == "owner":
        source.user_id = "b"
    else:
        source.status = "pending"
    sync_db_session.commit()
    with pytest.raises(ValidationError):
        await repo.resolve_effective_task(link)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"result": 2},
        {"inputs": {"x": 1}},
        {"status": "pending"},
        {"origin_type": TaskOriginType.create},
    ],
)
async def test_archive_blocks_supported_updates(
    sync_db_session: Session, fields: dict[str, Any]
) -> None:
    repo = TaskRepository(sync_db_session)
    task = await repo.create_task(name="Frozen", status="completed", result=1)
    await TaskCreator(sync_db_session).from_archive(task)
    with pytest.raises(ValidationError, match="[Aa]rchiv"):
        await repo.update_task(task.id, **fields)
    sync_db_session.refresh(task)
    assert task.result == 1
    assert task.origin_type == TaskOriginType.archive
    assert task.status == "completed"


@pytest.mark.asyncio
async def test_archive_not_claimed_or_marked_but_copy_is_editable(sync_db_session: Session) -> None:
    repo = TaskRepository(sync_db_session)
    task = await repo.create_task(name="Frozen", status="completed", result=1)
    tree = await TaskCreator(sync_db_session).from_archive(task)
    assert task.id not in TaskExecutor()._mark_tasks_for_reexecution(tree)
    assert await repo.try_claim_for_execution(task.id, ["completed"]) is None
    copied = await TaskCreator(sync_db_session).from_copy(task, _recursive=False)
    await repo.update_task(copied.task.id, result=2)
    assert task.result == 1


@pytest.mark.asyncio
async def test_archive_child_cannot_be_reset(sync_db_session: Session) -> None:
    repo = TaskRepository(sync_db_session)
    root = await repo.create_task(name="Root", status="completed")
    child = await repo.create_task(
        name="Frozen child", parent_id=root.id, status="completed", result=7
    )
    await TaskCreator(sync_db_session).from_archive(child, _recursive=False)
    await repo.reset_task_tree_for_reexecution(root.id)
    sync_db_session.refresh(child)
    assert child.status == "completed"
    assert child.result == 7


@pytest.mark.asyncio
async def test_cancel_running_without_acknowledgement_is_request(sync_db_session: Session) -> None:
    manager = TaskManager(sync_db_session)
    task = await manager.task_repository.create_task(name="Running", status="in_progress")
    response = await manager.cancel_task(task.id)
    sync_db_session.refresh(task)
    assert response["status"] == "cancelling"
    assert response["cancel_requested"] is True
    assert response["stop_confirmed"] is False
    assert task.cancel_requested_at is not None
    assert task.status == "in_progress"


@pytest.mark.asyncio
async def test_cancel_facade_notifies_actual_executor_and_event(sync_db_session: Session) -> None:
    facade = TaskExecutor()
    task = await TaskRepository(sync_db_session).create_task(name="Running", status="in_progress")
    executor = AsyncMock()
    executor.cancel.return_value = {"status": "cancelled", "partial_result": {"done": 1}}
    facade._executor_instances[task.id] = executor
    event = facade._cancellation_events.setdefault(task.id, asyncio.Event())
    response = await facade.cancel_task(task.id, db_session=sync_db_session)
    assert response["stop_confirmed"] is True
    assert event.is_set()
    executor.cancel.assert_awaited_once()
    sync_db_session.refresh(task)
    assert task.status == "cancelled"
    assert task.result == {"done": 1}
    facade._cancellation_events.pop(task.id, None)


@pytest.mark.asyncio
async def test_terminal_transition_ignores_cancelled_and_stale_attempt(
    sync_db_session: Session,
) -> None:
    repo = TaskRepository(sync_db_session)
    task = await repo.create_task(
        name="Cancelled", status="cancelled", result="partial", attempt_id=2
    )
    assert (
        await repo.transition_task(
            task.id,
            expected_statuses=["in_progress"],
            expected_attempt_id=1,
            status="failed",
            result="late",
        )
        is None
    )
    sync_db_session.refresh(task)
    assert task.status == "cancelled"
    assert task.result == "partial"


@pytest.mark.asyncio
async def test_requested_cancel_completion_confirms_without_late_result(
    sync_db_session: Session,
) -> None:
    manager = TaskManager(sync_db_session)
    task = await manager.task_repository.create_task(name="Running", status="in_progress")
    await manager.cancel_task(task.id)
    await manager._handle_task_execution_result(task, task.id, {"result": "late"})
    sync_db_session.refresh(task)
    assert task.status == "cancelled"
    assert task.result is None


@pytest.mark.asyncio
async def test_archive_orm_update_and_delete_are_guarded(sync_db_session: Session) -> None:
    repo = TaskRepository(sync_db_session)
    task = await repo.create_task(name="Frozen", status="completed", result=1)
    await TaskCreator(sync_db_session).from_archive(task)
    task.result = 2
    with pytest.raises(ValidationError, match="Archived"):
        sync_db_session.commit()
    sync_db_session.rollback()
    sync_db_session.delete(task)
    with pytest.raises(ValidationError, match="Archived"):
        sync_db_session.commit()
    sync_db_session.rollback()
    assert await repo.delete_task(task.id) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_request", [False, True])
async def test_actual_executor_stops_from_independent_cancel_request(
    sync_db_session: Session, remote_request: bool
) -> None:
    from apflow.core.base import BaseTask
    from apflow.core.extensions import get_registry

    started = asyncio.Event()
    release = asyncio.Event()
    notified: list[bool] = []

    class BlockingExecutor(BaseTask):
        id = "training_cancel_probe"
        name = "Blocking cancellation probe"
        description = "Wait until cancellation is received."

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            started.set()
            await release.wait()
            assert self.cancellation_checker is not None
            notified.append(self.cancellation_checker())
            return {"late": True}

        async def cancel(self) -> dict[str, Any]:
            release.set()
            return {"status": "cancelled", "partial_result": {"partial": True}}

    registry = get_registry()
    registry.register(BlockingExecutor(), executor_class=BlockingExecutor, override=True)
    facade = TaskExecutor()
    repo = TaskRepository(sync_db_session)
    task = await repo.create_task(name="Blocking", params={"executor_id": BlockingExecutor.id})
    task_id = str(task.id)
    run = asyncio.create_task(facade.execute_task_by_id(task_id, db_session=sync_db_session))
    try:
        await asyncio.wait_for(started.wait(), 3)
        with Session(bind=sync_db_session.get_bind()) as separate:
            if remote_request:
                await TaskRepository(separate).request_cancellation(task_id)
            else:
                response = await facade.cancel_task(task_id, db_session=separate)
                assert response["stop_confirmed"] is True
        await asyncio.wait_for(run, 3)
        sync_db_session.refresh(task)
        assert task.status == "cancelled"
        assert task.result == {"partial": True}
        assert notified == [True]
    finally:
        release.set()
        if not run.done():
            await asyncio.wait_for(run, 3)
        registry.unregister(BlockingExecutor.id)
        facade._cancellation_events.pop(task_id, None)
        facade._executor_instances.pop(task_id, None)


@pytest.mark.asyncio
async def test_cancellation_error_does_not_claim_stop(sync_db_session: Session) -> None:
    manager = TaskManager(sync_db_session)
    task = await manager.task_repository.create_task(name="Running", status="in_progress")
    executor = AsyncMock()
    executor.cancel.side_effect = RuntimeError("Unable to confirm downstream stop")
    manager._executor_instances[task.id] = executor
    response = await manager.cancel_task(task.id)
    assert response["status"] == "cancelling"
    assert response["stop_confirmed"] is False
    sync_db_session.refresh(task)
    assert task.status == "in_progress"


@pytest.mark.asyncio
async def test_engine_does_not_retry_unknown_delivery(sync_db_session: Session) -> None:
    from apflow.core.base import BaseTask
    from apflow.core.extensions import get_registry
    from apflow.durability.attempts import list_execution_attempts
    from apflow.durability.retry import RetryManager

    calls: list[int] = []

    class UnknownExecutor(BaseTask):
        id = "training_unknown_probe"
        name = "Unknown delivery probe"
        description = "Fail without claiming replay safety."

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            calls.append(1)
            raise RuntimeError("Remote response was not observed")

    registry = get_registry()
    registry.register(UnknownExecutor(), executor_class=UnknownExecutor, override=True)
    manager = TaskManager(sync_db_session, retry_manager=RetryManager())
    task = await manager.task_repository.create_task(
        name="Unknown outcome", max_attempts=3, params={"executor_id": UnknownExecutor.id}
    )
    try:
        await manager._execute_single_task(task, use_callback=False)
        sync_db_session.refresh(task)
        assert len(calls) == 1
        assert task.status == "failed"
        assert task.error_info is not None
        assert task.error_info["delivery_state"] == "unknown"
        history = await list_execution_attempts(task.id, sync_db_session)
        assert len(history) == 1
        assert history[0]["status"] == "unknown"
    finally:
        registry.unregister(UnknownExecutor.id)


@pytest.mark.asyncio
async def test_engine_reserves_and_accounts_each_retry(sync_db_session: Session) -> None:
    from apflow.core.base import BaseTask
    from apflow.core.extensions import get_registry
    from apflow.durability.attempts import list_execution_attempts
    from apflow.durability.retry import RetryManager
    from apflow.governance.ledger import BudgetLedger

    calls: list[int] = []

    class MeteredExecutor(BaseTask):
        id = "training_metered_probe"
        name = "Metered retry probe"
        description = "Report every known retry cost."
        result_format = "envelope"

        def token_reservation_bound(self, inputs: dict[str, Any]) -> int:
            return 3

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            calls.append(1)
            if len(calls) == 1:
                return {
                    "result": None,
                    "error": "Transient response",
                    "token_usage": {"total": 2},
                    "error_info": {
                        "version": 1,
                        "code": "TEMPORARY",
                        "category": "transient",
                        "retryable": True,
                        "delivery_state": "not_sent",
                        "origin": "test",
                        "phase": "execution",
                        "observed_at": "2026-10-24T00:00:00+00:00",
                    },
                }
            return {"result": {"ok": True}, "token_usage": {"total": 3}}

    registry = get_registry()
    registry.register(MeteredExecutor(), executor_class=MeteredExecutor, override=True)
    ledger = BudgetLedger(sync_db_session)
    await ledger.configure_scope("retry-scope", 5)
    manager = TaskManager(sync_db_session, retry_manager=RetryManager())
    task = await manager.task_repository.create_task(
        name="Metered",
        max_attempts=2,
        budget_scope_id="retry-scope",
        params={"executor_id": MeteredExecutor.id},
    )
    try:
        await manager._execute_single_task(task, use_callback=False)
        sync_db_session.refresh(task)
        assert task.status == "completed"
        assert task.token_usage["total"] == 5
        assert (await ledger.get_scope("retry-scope")).spent_tokens == 5
        history = await list_execution_attempts(task.id, sync_db_session)
        assert len(history) == 2
        failed = next(item for item in history if item["status"] == "failed")
        assert failed["token_usage"]["total"] == 2
        assert failed["next_retry_at"] is not None
    finally:
        registry.unregister(MeteredExecutor.id)


@pytest.mark.asyncio
async def test_cancel_wakes_retry_backoff(sync_db_session: Session) -> None:
    from apflow.core.base import BaseTask
    from apflow.core.extensions import get_registry
    from apflow.durability.attempts import list_execution_attempts
    from apflow.durability.retry import RetryManager

    calls: list[int] = []

    class BackoffExecutor(BaseTask):
        id = "training_backoff_probe"
        name = "Backoff cancellation probe"
        description = "Return a retry-safe temporary failure."
        result_format = "envelope"

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            calls.append(1)
            return {
                "result": None,
                "error": "Retry later",
                "error_info": {
                    "version": 1,
                    "code": "TEMPORARY",
                    "category": "transient",
                    "retryable": True,
                    "delivery_state": "not_sent",
                    "origin": "test",
                    "phase": "execution",
                    "observed_at": "2026-10-24T00:00:00+00:00",
                    "retry_after_seconds": 5,
                },
            }

    registry = get_registry()
    registry.register(BackoffExecutor(), executor_class=BackoffExecutor, override=True)
    manager = TaskManager(sync_db_session, retry_manager=RetryManager())
    task = await manager.task_repository.create_task(
        name="Backoff", max_attempts=2, params={"executor_id": BackoffExecutor.id}
    )
    run = asyncio.create_task(manager._execute_single_task(task, use_callback=False))
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            records = await list_execution_attempts(task.id, sync_db_session)
            if records and records[0]["next_retry_at"]:
                break
        else:
            pytest.fail("Task did not reach retry backoff")
        await manager.cancel_task(task.id)
        await asyncio.wait_for(run, 1)
        sync_db_session.refresh(task)
        assert task.status == "cancelled"
        assert calls == [1]
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        registry.unregister(BackoffExecutor.id)


@pytest.mark.asyncio
async def test_link_archive_materializes_frozen_result_and_execution_config(
    sync_db_session: Session,
) -> None:
    repository = TaskRepository(sync_db_session)
    creator = TaskCreator(sync_db_session)
    original = await repository.create_task(
        name="Source",
        status="completed",
        user_id="alice",
        result={"version": 1},
        params={"executor_id": "rest_executor"},
        inputs={"url": "https://example.com"},
    )
    linked = await creator.from_link(original, _recursive=False)
    linked_id = linked.task.id
    archive = await creator.from_archive(linked.task)
    assert archive.task.id == linked_id
    assert archive.task.original_task_id == original.id
    assert archive.task.user_id == "alice"
    assert archive.task.result == {"version": 1}
    assert archive.task.params == {"executor_id": "rest_executor"}
    await repository.update_task(original.id, result={"version": 2})
    assert (await repository.get_task_tree_for_api(archive.task)).task.result == {"version": 1}
    original.result["version"] = 3
    assert archive.task.result == {"version": 1}


@pytest.mark.asyncio
async def test_mixed_archive_preserves_link_ids_dependencies_and_snapshot(
    sync_db_session: Session,
) -> None:
    repository = TaskRepository(sync_db_session)
    creator = TaskCreator(sync_db_session)
    source_tree = await creator.create_task_tree_from_array(
        [
            {
                "id": "consumer",
                "name": "Consumer",
                "user_id": "alice",
                "status": "completed",
                "dependencies": ["data"],
            },
            {
                "id": "data",
                "name": "Data",
                "parent_id": "consumer",
                "user_id": "alice",
                "status": "completed",
                "result": {"version": 1},
            },
        ]
    )
    source = source_tree.children[0].task
    mixed = await creator.from_mixed(source_tree.task, _link_task_ids=[source.id])
    linked = mixed.children[0].task
    # from_copy intentionally resets status to pending; archive a completed mixed run.
    await repository.update_task(mixed.task.id, status="completed")
    expected_dependencies = mixed.task.dependencies
    archived = await creator.from_archive(mixed.task)
    assert archived.task.dependencies == expected_dependencies
    assert archived.children[0].task.id == linked.id
    assert archived.children[0].task.result == {"version": 1}
    await repository.update_task(source.id, result={"version": 2})
    inputs = await TaskManager(sync_db_session).resolve_task_dependencies(archived.task)
    assert inputs[linked.id] == {"version": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "cycle", "owner"])
async def test_recursive_archive_reference_failure_is_atomic(
    sync_db_session: Session, failure: str
) -> None:
    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="Root", status="completed", user_id="alice")
    source = await repository.create_task(name="Source", status="completed", user_id="bob")
    child = await repository.create_task(
        name="Broken link",
        parent_id=root.id,
        status="completed",
        user_id="alice",
        origin_type="link",
        original_task_id="missing" if failure == "missing" else source.id,
    )
    if failure == "cycle":
        await repository.update_task(child.id, original_task_id=child.id)
    with pytest.raises(ValidationError):
        await TaskCreator(sync_db_session).from_archive(root)
    sync_db_session.refresh(root)
    sync_db_session.refresh(child)
    assert root.origin_type != TaskOriginType.archive
    assert child.origin_type == TaskOriginType.link


@pytest.mark.asyncio
@pytest.mark.parametrize("write_path", ["create", "orm", "reparent"])
async def test_archive_rejects_new_or_reparented_children(
    sync_db_session: Session,
    write_path: str,
) -> None:
    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="Frozen root", status="completed")
    await TaskCreator(sync_db_session).from_archive(root)
    if write_path == "create":
        with pytest.raises(ValidationError, match="[Aa]rchiv"):
            await repository.create_task(name="New child", parent_id=root.id)
    elif write_path == "orm":
        sync_db_session.add(TaskModel(name="New child", parent_id=root.id))
        with pytest.raises(ValidationError, match="[Aa]rchiv"):
            sync_db_session.commit()
        sync_db_session.rollback()
    else:
        child = await repository.create_task(name="Existing child")
        with pytest.raises(ValidationError, match="[Aa]rchiv"):
            await repository.update_task(child.id, parent_id=root.id)


@pytest.mark.asyncio
async def test_link_copy_materializes_modifiable_configuration(sync_db_session: Session) -> None:
    repository = TaskRepository(sync_db_session)
    creator = TaskCreator(sync_db_session)
    source = await repository.create_task(
        name="Source",
        user_id="alice",
        status="completed",
        result={"version": 1},
        params={"executor_id": "rest_executor"},
        inputs={"url": "https://example.com"},
    )
    linked = await creator.from_link(source, _recursive=False)
    copied = await creator.from_copy(linked.task, _recursive=False)
    assert copied.task.params == source.params
    assert copied.task.inputs == source.inputs
    assert copied.task.result is None
    assert copied.task.status == "pending"
    await repository.update_task(copied.task.id, result={"version": 2})
    assert source.result == {"version": 1}


@pytest.mark.asyncio
async def test_archive_serializes_concurrent_child_insert(
    sync_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="Frozen", status="completed")
    root_id = str(root.id)
    creator = TaskCreator(sync_db_session)
    started = threading.Event()
    insertion: asyncio.Task[str] | None = None
    lock = creator.task_repository.lock_tasks

    def insert_child() -> str:
        with Session(bind=sync_db_session.get_bind()) as session:
            started.set()
            try:
                asyncio.run(
                    TaskRepository(session).create_task(name="Late child", parent_id=root_id)
                )
            except ValidationError:
                return "rejected"
        return "inserted"

    async def lock_and_start_writer(task_ids: list[str]) -> None:
        nonlocal insertion
        await lock(task_ids)
        insertion = asyncio.create_task(asyncio.to_thread(insert_child))
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0.05)
        assert not insertion.done()

    monkeypatch.setattr(creator.task_repository, "lock_tasks", lock_and_start_writer)
    await creator.from_archive(root)
    assert insertion is not None
    assert await asyncio.wait_for(insertion, 3) == "rejected"
    assert await repository.get_child_tasks_by_parent_id(root_id) == []


@pytest.mark.asyncio
async def test_single_node_archive_preserves_existing_child_membership(
    sync_db_session: Session,
) -> None:
    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="Root", status="completed")
    child = await repository.create_task(name="Child", parent_id=root.id, status="completed")
    await TaskCreator(sync_db_session).from_archive(root, _recursive=False)
    with pytest.raises(ValidationError, match="[Aa]rchiv"):
        await repository.update_task(child.id, parent_id=None)
    assert await repository.delete_task(child.id) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_kind", ["dispatch", "lease"])
async def test_unowned_executor_cannot_replay_claimed_tree(
    sync_db_session: Session,
    owner_kind: str,
) -> None:
    from datetime import datetime, timedelta, timezone
    from apflow.core.base import BaseTask
    from apflow.core.extensions import get_registry
    from apflow.core.storage.sqlalchemy.models import DistributedNode, TaskDispatch, TaskLease

    calls: list[str] = []

    class OwnedExecutor(BaseTask):
        id = "training_owned_probe"
        name = "Ownership probe"
        description = "Record accidental replay of an owned tree."

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            calls.append(inputs["step"])
            return {"value": "replayed"}

    registry = get_registry()
    registry.register(OwnedExecutor(), executor_class=OwnedExecutor, override=True)
    creator = TaskCreator(sync_db_session)
    tree = await creator.create_task_tree_from_array(
        [
            {
                "name": "root",
                "params": {"executor_id": OwnedExecutor.id},
                "inputs": {"step": "root"},
            },
            {
                "name": "child",
                "parent_id": "root",
                "status": "completed",
                "result": {"value": "cached"},
                "params": {"executor_id": OwnedExecutor.id},
                "inputs": {"step": "child"},
            },
        ]
    )
    if owner_kind == "dispatch":
        sync_db_session.add(TaskDispatch(task_id=tree.task.id, state="accepted"))
    else:
        sync_db_session.add(
            DistributedNode(
                node_id="owner",
                executor_types=[],
                capabilities={},
                status="healthy",
                heartbeat_at=datetime.now(timezone.utc),
            )
        )
        sync_db_session.add(
            TaskLease(
                task_id=tree.task.id,
                node_id="owner",
                lease_token="owner-token",
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            )
        )
    sync_db_session.commit()
    try:
        await TaskExecutor().execute_task_by_id(tree.task.id, db_session=sync_db_session)
        assert calls == []
        sync_db_session.refresh(tree.children[0].task)
        assert tree.children[0].task.result == {"value": "cached"}
        # Out-of-band cancellation remains authorized without an execution lease.
        cancelled = await TaskExecutor().cancel_task_tree(tree.task.id, db_session=sync_db_session)
        assert cancelled["stop_confirmed"] is True
    finally:
        registry.unregister(OwnedExecutor.id)
