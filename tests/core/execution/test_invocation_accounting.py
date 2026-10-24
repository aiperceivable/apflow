"""Engine-level accounting remains correct across cancellation and retries."""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from apflow.core.execution.task_manager import TaskManager
from apflow.core.execution.result_protocol import ExecutorResult
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


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
