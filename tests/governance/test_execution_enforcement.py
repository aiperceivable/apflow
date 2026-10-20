"""Exercise governance decisions and accounting through the real execution path."""

import json
import logging
from collections.abc import Iterator
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from apflow.core.base import BaseTask
from apflow.core.execution.task_manager import TaskManager
from apflow.core.extensions import get_registry
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.governance import BudgetManager, CostPolicy, PolicyAction, PolicyEngine, UsageReporter


@pytest.fixture
def governance_calls() -> Iterator[list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    class GovernanceProbeExecutor(BaseTask):
        id = "governance_test_probe"
        name = "Governance probe"
        description = "Record inputs and return a supplied execution result."

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            calls.append(dict(inputs))
            return inputs.get("execution_result", {"observed_model": inputs.get("model")})

    registry = get_registry()
    registry.register(GovernanceProbeExecutor(), executor_class=GovernanceProbeExecutor)
    try:
        yield calls
    finally:
        registry.unregister(GovernanceProbeExecutor.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("used", [79, 80, 90, 100])
@pytest.mark.parametrize("action", list(PolicyAction))
async def test_execution_applies_policy_at_configured_threshold(
    sync_db_session: Session,
    governance_calls: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
    used: int,
    action: PolicyAction,
) -> None:
    repository = TaskRepository(sync_db_session)
    engine = PolicyEngine()
    engine.register_policy(
        CostPolicy(
            name="threshold_policy",
            action=action,
            threshold=0.8,
            downgrade_chain=["original", "cheaper"],
        )
    )
    manager = TaskManager(
        sync_db_session,
        pre_hooks=[],
        post_hooks=[],
        budget_manager=BudgetManager(repository),
        policy_engine=engine,
    )
    task = await repository.create_task(
        name="Governed execution",
        params={"executor_id": "governance_test_probe"},
        inputs={"model": "original"},
        token_budget=100,
        token_usage={"total": used},
        cost_policy="threshold_policy",
    )

    with caplog.at_level(logging.WARNING):
        await manager._execute_single_task(task, use_callback=False)

    refreshed = await repository.get_task_by_id(str(task.id))
    assert refreshed is not None
    blocked = used >= 80 and action == PolicyAction.BLOCK
    assert str(refreshed.status) == ("failed" if blocked else "completed")
    assert len(governance_calls) == (0 if blocked else 1)
    if governance_calls:
        downgraded = used >= 80 and action == PolicyAction.DOWNGRADE
        assert governance_calls[0]["model"] == ("cheaper" if downgraded else "original")
        if downgraded:
            assert cast(dict[str, Any], refreshed.params)["_downgrade_model_index"] == 1
    warned = any("Budget warning:" in record.message for record in caplog.records)
    assert warned == (used >= 80 and action == PolicyAction.NOTIFY)


@pytest.mark.asyncio
@pytest.mark.parametrize("used", [50, 100])
async def test_execution_rejects_unknown_policy(
    sync_db_session: Session,
    governance_calls: list[dict[str, Any]],
    used: int,
) -> None:
    repository = TaskRepository(sync_db_session)
    manager = TaskManager(
        sync_db_session,
        pre_hooks=[],
        post_hooks=[],
        budget_manager=BudgetManager(repository),
        policy_engine=PolicyEngine(),
    )
    task = await repository.create_task(
        name="Invalid policy",
        params={"executor_id": "governance_test_probe"},
        token_budget=100,
        token_usage={"total": used},
        cost_policy="missing_policy",
    )
    await manager._execute_single_task(task, use_callback=False)
    refreshed = await repository.get_task_by_id(str(task.id))
    assert refreshed is not None
    assert str(refreshed.status) == "failed"
    assert "missing_policy" in str(refreshed.error)
    assert governance_calls == []


@pytest.mark.asyncio
async def test_execution_skips_policy_for_unlimited_budget(
    sync_db_session: Session, governance_calls: list[dict[str, Any]]
) -> None:
    repository = TaskRepository(sync_db_session)
    manager = TaskManager(
        sync_db_session,
        pre_hooks=[],
        post_hooks=[],
        budget_manager=BudgetManager(repository),
        policy_engine=PolicyEngine(),
    )
    task = await repository.create_task(
        name="Unlimited budget",
        params={"executor_id": "governance_test_probe"},
        cost_policy="unused_policy",
    )
    await manager._execute_single_task(task, use_callback=False)
    refreshed = await repository.get_task_by_id(str(task.id))
    assert refreshed is not None
    assert str(refreshed.status) == "completed"
    assert len(governance_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "Provider returned a partial failure"])
async def test_execution_accumulates_reported_usage_on_success_and_failure(
    sync_db_session: Session,
    governance_calls: list[dict[str, Any]],
    error: str | None,
) -> None:
    repository = TaskRepository(sync_db_session)
    manager = TaskManager(
        sync_db_session,
        pre_hooks=[],
        post_hooks=[],
        budget_manager=BudgetManager(repository),
    )
    result = {"error": error, "token_usage": {"input": 10, "output": 10, "total": 20}}
    task = await repository.create_task(
        name="Accounted execution",
        params={"executor_id": "governance_test_probe"},
        inputs={"execution_result": result},
        token_budget=100,
        token_usage={"input": 20, "output": 20, "total": 40},
    )
    await manager._execute_single_task(task, use_callback=False)
    refreshed = await repository.get_task_by_id(str(task.id))
    assert refreshed is not None
    assert str(refreshed.status) == ("failed" if error else "completed")
    assert refreshed.token_usage == {"input": 30, "output": 30, "total": 60}
    assert refreshed.result == result
    assert (await UsageReporter(repository).get_task_usage(str(task.id))).total_tokens == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_cost", [Decimal("0"), Decimal("0.012345")])
async def test_application_reported_cost_persists_and_exports(
    sync_db_session: Session, actual_cost: Decimal
) -> None:
    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="Priced execution", user_id="cost_owner")
    assert task.to_dict()["actual_cost_usd"] is None
    assert task.to_dict()["estimated_cost_usd"] is None
    estimated_cost = Decimal("0") if actual_cost == 0 else Decimal("0.02")
    await repository.update_task(
        str(task.id), estimated_cost_usd=estimated_cost, actual_cost_usd=actual_cost
    )
    sync_db_session.expire_all()
    refreshed = await repository.get_task_by_id(str(task.id))
    assert refreshed is not None
    assert refreshed.to_dict()["estimated_cost_usd"] == float(estimated_cost)
    assert refreshed.to_dict()["actual_cost_usd"] == float(actual_cost)
    reporter = UsageReporter(repository)
    assert (await reporter.get_task_usage(str(task.id))).total_cost_usd == float(actual_cost)
    summary = await reporter.get_user_usage("cost_owner")
    assert summary.task_count == 1
    assert json.loads(reporter.export_json(summary))["total_cost_usd"] == float(actual_cost)
