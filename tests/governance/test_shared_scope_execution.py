"""Exercise shared reservations and retry accounting through the real TaskManager."""

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.orm import Session

from apflow.core.base import BaseTask
from apflow.core.execution.errors import ValidationError, http_error_info
from apflow.core.execution.task_manager import TaskManager
from apflow.core.extensions import get_registry
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.durability.attempts import list_execution_attempts
from apflow.durability.retry import RetryManager
from apflow.governance.budget import BudgetManager
from apflow.governance.ledger import BudgetLedger


@pytest.fixture
def bounded_calls() -> Iterator[list[str]]:
    calls: list[str] = []

    class BoundedProbe(BaseTask):
        id = "shared_budget_probe"
        name = "Shared budget probe"
        description = "Exercise a registered executor's enforced invocation bound."
        result_format = "envelope"

        def token_reservation_bound(self, inputs: dict[str, Any]) -> int | None:
            return None if inputs.get("unsupported") else 40

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            calls.append(inputs.get("behavior", "retry"))
            if inputs.get("behavior") == "invalid":
                raise ValidationError("Invalid input", token_usage={"total": 10})
            if inputs.get("behavior") == "unknown":
                raise RuntimeError("External receipt was lost")
            if len(calls) == 1:
                return {
                    "result": None,
                    "error": "Unavailable",
                    "error_info": http_error_info(503, "GET"),
                    "token_usage": {"total": 30},
                }
            return {"result": {"done": True}, "token_usage": {"total": 30}}

    registry = get_registry()
    registry.register(BoundedProbe(), executor_class=BoundedProbe)
    try:
        yield calls
    finally:
        registry.unregister(BoundedProbe.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit,expected_calls,expected_status", [(100, 2, "completed"), (50, 1, "failed")]
)
async def test_each_retry_reserves_and_accounts_against_shared_scope(
    sync_db_session: Session,
    bounded_calls: list[str],
    limit: int,
    expected_calls: int,
    expected_status: str,
) -> None:
    repository = TaskRepository(sync_db_session)
    ledger = BudgetLedger(sync_db_session)
    await ledger.configure_scope("shared", limit)
    manager = TaskManager(
        sync_db_session,
        pre_hooks=[],
        post_hooks=[],
        retry_manager=RetryManager(),
        budget_manager=BudgetManager(repository),
    )
    task = await repository.create_task(
        name="Budgeted task",
        params={"executor_id": "shared_budget_probe"},
        budget_scope_id="shared",
        max_attempts=3,
        backoff_base_seconds=0.1,
    )
    await manager._execute_single_task(task)
    refreshed = await repository.get_task_by_id(str(task.id))
    assert refreshed is not None and refreshed.status == expected_status
    assert len(bounded_calls) == expected_calls
    assert refreshed.token_usage["total"] == 30 * expected_calls
    snapshot = await ledger.get_scope("shared")
    assert snapshot.spent_tokens == 30 * expected_calls and snapshot.reserved_tokens == 0
    history = await list_execution_attempts(str(task.id), sync_db_session)
    assert len(history) == 2
    assert history[-1]["error_info"]["code"] == "HTTP_503"
    assert history[-1]["next_retry_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "behavior,unknown_tokens,spent_tokens", [("invalid", 0, 10), ("unknown", 40, 0)]
)
async def test_permanent_and_unknown_errors_do_not_replay_model_invocation(
    sync_db_session: Session,
    bounded_calls: list[str],
    behavior: str,
    unknown_tokens: int,
    spent_tokens: int,
) -> None:
    repository = TaskRepository(sync_db_session)
    ledger = BudgetLedger(sync_db_session)
    await ledger.configure_scope("shared", 100)
    manager = TaskManager(
        sync_db_session,
        pre_hooks=[],
        post_hooks=[],
        retry_manager=RetryManager(),
        budget_manager=BudgetManager(repository),
    )
    task = await repository.create_task(
        name="Budgeted task",
        params={"executor_id": "shared_budget_probe"},
        inputs={"behavior": behavior},
        budget_scope_id="shared",
        max_attempts=3,
    )
    await manager._execute_single_task(task)
    assert len(bounded_calls) == 1
    snapshot = await ledger.get_scope("shared")
    assert snapshot.unknown_tokens == unknown_tokens and snapshot.spent_tokens == spent_tokens
    history = await list_execution_attempts(str(task.id), sync_db_session)
    assert len(history) == 1 and history[0]["error_info"] is not None


@pytest.mark.asyncio
async def test_scoped_task_fails_closed_without_optional_governance_manager(
    sync_db_session: Session,
    bounded_calls: list[str],
) -> None:
    repository = TaskRepository(sync_db_session)
    await BudgetLedger(sync_db_session).configure_scope("shared", 100)
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    task = await repository.create_task(
        name="Unsupported bound",
        params={"executor_id": "shared_budget_probe"},
        inputs={"unsupported": True},
        budget_scope_id="shared",
    )
    await manager._execute_single_task(task)
    assert bounded_calls == []
    current = await repository.get_task_by_id(str(task.id))
    assert current is not None and current.status == "failed"
