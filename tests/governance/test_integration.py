"""Governance wiring + real-session integration (F-004).

These cover what the isolated unit tests never did: that the async BudgetManager
works against a REAL TaskRepository (the previous sync-MagicMock tests masked the
missing await), and that TaskExecutor assembles the governance components when the
app enables governance — so every execution path (module/REST/MCP/A2A/scheduler)
is consistently governed.
"""

import pytest

from apflow.core.config import (
    clear_config,
    set_governance_enabled,
    set_policy_engine,
)
from apflow.core.execution.task_executor import TaskExecutor
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.governance.budget import BudgetManager
from apflow.governance.policy import CostPolicy, PolicyAction, PolicyEngine


# --- Wiring: TaskExecutor builds governance components only when enabled ---


def test_build_components_governance_enabled(sync_db_session):
    clear_config()
    try:
        set_governance_enabled(True)
        set_policy_engine(PolicyEngine())
        components = TaskExecutor()._build_execution_components(sync_db_session)
        assert "budget_manager" in components
        assert isinstance(components["budget_manager"], BudgetManager)
        assert isinstance(components["policy_engine"], PolicyEngine)
    finally:
        clear_config()


def test_build_components_disabled_by_default(sync_db_session):
    clear_config()
    try:
        components = TaskExecutor()._build_execution_components(sync_db_session)
        assert components == {}  # no governance/durability unless the app opts in
    finally:
        clear_config()


# --- Real async BudgetManager against a real repository (not a sync mock) ---


@pytest.mark.asyncio
async def test_budget_check_allows_under_limit(sync_db_session):
    repo = TaskRepository(sync_db_session)
    task = await repo.create_task(name="t", token_budget=1000, token_usage={"total": 400})
    result = await BudgetManager(repo).check_budget(task.id)
    assert result.allowed is True
    assert result.remaining == 600


@pytest.mark.asyncio
async def test_budget_check_blocks_when_exhausted(sync_db_session):
    repo = TaskRepository(sync_db_session)
    task = await repo.create_task(name="t", token_budget=100, token_usage={"total": 100})
    result = await BudgetManager(repo).check_budget(task.id)
    assert result.allowed is False
    assert result.remaining == 0


@pytest.mark.asyncio
async def test_budget_check_unlimited_without_budget(sync_db_session):
    repo = TaskRepository(sync_db_session)
    task = await repo.create_task(name="t")  # no token_budget set
    result = await BudgetManager(repo).check_budget(task.id)
    assert result.allowed is True
    assert result.remaining == -1  # unlimited


@pytest.mark.asyncio
async def test_update_usage_accumulates_and_persists(sync_db_session):
    repo = TaskRepository(sync_db_session)
    task = await repo.create_task(name="t", token_budget=1000, token_usage={"total": 300})
    await BudgetManager(repo).update_usage(task.id, {"input": 50, "output": 50, "total": 100})
    refreshed = await repo.get_task_by_id(task.id)
    assert refreshed is not None
    assert refreshed.token_usage["total"] == 400


# --- Policy evaluation drives the block/downgrade decision on exhaustion ---


def test_policy_blocks_on_exhaustion():
    engine = PolicyEngine()
    engine.register_policy(CostPolicy(name="strict", action=PolicyAction.BLOCK, threshold=0.8))
    evaluation = engine.evaluate("strict", utilization=1.0)
    assert evaluation.action == PolicyAction.BLOCK


def test_policy_downgrades_to_next_model():
    engine = PolicyEngine()
    engine.register_policy(
        CostPolicy(
            name="downgrade",
            action=PolicyAction.DOWNGRADE,
            threshold=0.7,
            downgrade_chain=["opus", "sonnet", "haiku"],
        )
    )
    evaluation = engine.evaluate("downgrade", utilization=0.85, current_model_index=0)
    assert evaluation.action == PolicyAction.DOWNGRADE
    assert evaluation.model_override == "sonnet"


# --- End-to-end enforcement: a budget-exhausted task is blocked during execution ---


@pytest.mark.asyncio
async def test_budget_exhausted_task_blocked_end_to_end(sync_db_session):
    """A token_budget-exhausted task executed through TaskManager (the exact
    governance wiring TaskExecutor builds per-execution) is BLOCKED — marked
    failed with a budget error before the executor runs. This closes the gap
    between 'BudgetManager.check_budget returns not-allowed' and 'the execution
    path actually enforces it'.
    """
    from apflow.core.execution.task_manager import TaskManager
    from apflow.core.types import TaskTreeNode

    repo = TaskRepository(sync_db_session)
    task_manager = TaskManager(
        sync_db_session,
        budget_manager=BudgetManager(repo),
        policy_engine=PolicyEngine(),
    )

    task = await task_manager.task_repository.create_task(
        name="budgeted",
        user_id="u1",
        inputs={"resource": "cpu"},
        schemas={"method": "aggregate_results_executor"},
        token_budget=100,
        token_usage={"total": 100},  # exhausted (used == limit)
    )

    await task_manager.distribute_task_tree(TaskTreeNode(task), use_callback=False)

    refreshed = await task_manager.task_repository.get_task_by_id(task.id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert "budget" in (refreshed.error or "").lower()
