"""Engine-level accounting remains correct across cancellation and retries."""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from apflow.core.execution.task_manager import TaskManager
from apflow.core.execution.result_protocol import ExecutorResult, ResultFormat
from apflow.core.base import BaseTask
from apflow.core.extensions import get_registry
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


class UsageEvidenceProbe(BaseTask):
    id = "usage_evidence_probe"
    name = "Usage evidence probe"
    description = "Return the supplied usage evidence through the actual execution path."
    result_format = "envelope"

    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"result": "done", "token_usage": inputs["usage"]}

    def token_reservation_bound(self, inputs: dict[str, Any]) -> int:
        return 10


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_budget", [False, True])
@pytest.mark.parametrize(
    "result_format,usage,known",
    [
        ("envelope", {}, False),
        ("legacy", {}, False),
        ("raw", {}, False),
        ("envelope", {"total": 0}, True),
        ("legacy", {"total": 0}, True),
    ],
)
async def test_engine_preserves_unknown_usage_and_explicit_zero(
    sync_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    shared_budget: bool,
    result_format: ResultFormat,
    usage: dict[str, int],
    known: bool,
) -> None:
    from sqlalchemy import select
    from apflow.core.storage.sqlalchemy.models import ExecutionAttempt, UsageEvent
    from apflow.durability.attempts import AttemptRecorder
    from apflow.governance.ledger import BudgetLedger
    from apflow.governance.reporter import UsageReporter

    monkeypatch.setattr(UsageEvidenceProbe, "result_format", result_format)
    registry = get_registry()
    registry.register(UsageEvidenceProbe(), executor_class=UsageEvidenceProbe, override=True)
    try:
        repository = TaskRepository(sync_db_session)
        task = await repository.create_task(
            name="Usage evidence",
            params={"executor_id": UsageEvidenceProbe.id},
            inputs={"usage": usage},
            budget_scope_id="scope" if shared_budget else None,
        )
        ledger = BudgetLedger(sync_db_session)
        if shared_budget:
            await ledger.configure_scope("scope", 100)
        await TaskManager(sync_db_session)._execute_single_task(task)
        sync_db_session.refresh(task)
        invocation = sync_db_session.scalars(select(ExecutionAttempt)).one()
        assert task.status == "completed"
        assert invocation.token_usage == ({"total": 0} if known else None)
        assert len(sync_db_session.scalars(select(UsageEvent)).all()) == int(known)
        report = await UsageReporter(repository).get_task_usage(task.id)
        assert report.unknown_usage_task_count == int(not known)
        if shared_budget:
            assert (await ledger.get_scope("scope")).unknown_tokens == (0 if known else 10)
        if not known:
            await AttemptRecorder(sync_db_session).finish(
                invocation.id, "completed", token_usage={"total": 5}
            )
            report = await UsageReporter(repository).get_task_usage(task.id)
            assert report.total_tokens == 5 and report.unknown_usage_task_count == 0
            if shared_budget:
                balance = await ledger.get_scope("scope")
                assert balance.spent_tokens == 5 and balance.unknown_tokens == 0
    finally:
        registry.unregister(UsageEvidenceProbe.id)


@pytest.mark.asyncio
async def test_repeated_cancel_does_not_repeat_cumulative_usage(sync_db_session: Session) -> None:
    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="Running", status="in_progress", attempt_id=1)
    executor = AsyncMock()
    executor.cancel.return_value = {"status": "cancelling", "token_usage": {"total": 2}}
    manager = TaskManager(sync_db_session, executor_instances={task.id: executor})
    await manager.cancel_task(task.id)
    await manager.cancel_task(task.id)
    sync_db_session.refresh(task)
    assert executor.cancel.await_count == 2
    assert task.token_usage is not None
    assert task.token_usage["total"] == 2


@pytest.mark.asyncio
async def test_cancel_does_not_finalize_task_before_usage_is_persisted(
    sync_db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apflow.durability.attempts import AttemptRecorder

    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="Running", status="in_progress", attempt_id=1)
    task_id = task.id
    executor = AsyncMock()
    executor.cancel.return_value = {"status": "cancelled", "token_usage": {"total": 2}}
    manager = TaskManager(sync_db_session, executor_instances={task_id: executor})
    original = AttemptRecorder.record_partial

    async def fail_usage(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Usage storage unavailable")

    monkeypatch.setattr(AttemptRecorder, "record_partial", fail_usage)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await manager.cancel_task(task_id)
    sync_db_session.refresh(task)
    assert task.status == "in_progress"
    assert task.cancel_requested_at is not None
    monkeypatch.setattr(AttemptRecorder, "record_partial", original)
    await manager.cancel_task(task_id)
    sync_db_session.refresh(task)
    assert task.status == "cancelled"
    assert task.token_usage is not None and task.token_usage["total"] == 2


@pytest.mark.asyncio
async def test_engine_finalizes_invocation_without_duplicate_projection(
    sync_db_session: Session,
) -> None:
    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="Call", status="in_progress", attempt_id=1)
    manager = TaskManager(sync_db_session)

    async def invoke(task: Any, inputs: dict[str, Any]) -> ExecutorResult:
        return {"result": "done", "token_usage": {"total": 5}}

    manager._execute_task_with_schemas = invoke
    result = await manager._execute_attempt(task, {})
    await manager._handle_task_execution_result(task, task.id, result, usage_recorded=True)
    sync_db_session.refresh(task)
    assert task.token_usage is not None
    assert task.token_usage["total"] == 5


@pytest.mark.asyncio
async def test_completed_run_parent_still_schedules_pending_child(sync_db_session: Session) -> None:
    from apflow.core.types import TaskTreeNode

    repository = TaskRepository(sync_db_session)
    root = await repository.create_task(name="Reference", status="completed", run_id="run")
    child = await repository.create_task(name="Pending", parent_id=root.id, run_id="run")
    tree = TaskTreeNode(root)
    tree.add_child(TaskTreeNode(child))
    manager = TaskManager(sync_db_session)

    async def complete(task: Any, use_callback: bool) -> None:
        task.status = "completed"

    manager._execute_single_task = AsyncMock(side_effect=complete)
    await manager._execute_task_tree_recursive(tree)
    manager._execute_single_task.assert_awaited_once_with(child, use_callback=True)


@pytest.mark.asyncio
async def test_budget_and_engine_share_one_invocation(sync_db_session: Session) -> None:
    from sqlalchemy import select
    from apflow.core.storage.sqlalchemy.models import (
        BudgetReservation,
        ExecutionAttempt,
        UsageEvent,
    )
    from apflow.governance.budget import BudgetManager
    from apflow.governance.ledger import BudgetLedger

    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(
        name="Bounded", status="in_progress", attempt_id=1, budget_scope_id="scope"
    )
    ledger = BudgetLedger(sync_db_session)
    await ledger.configure_scope("scope", 100)
    manager = TaskManager(sync_db_session)

    async def invoke(task: Any, inputs: dict[str, Any]) -> ExecutorResult:
        async def consume() -> ExecutorResult:
            return {"result": "done", "token_usage": {"total": 5}}

        return await BudgetManager(repository).execute_with_reservation(task, consume, 10)

    manager._execute_task_with_schemas = invoke
    await manager._execute_attempt(task, {})
    attempts = list(sync_db_session.scalars(select(ExecutionAttempt)))
    events = list(sync_db_session.scalars(select(UsageEvent)))
    reservations = list(sync_db_session.scalars(select(BudgetReservation)))
    assert len(attempts) == len(events) == len(reservations) == 1
    assert attempts[0].id == reservations[0].id == events[0].invocation_id
    balance = await ledger.get_scope("scope")
    assert balance.spent_tokens == 5 and balance.reserved_tokens == 0
