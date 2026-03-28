"""Tests for budget module"""

import pytest
from unittest.mock import MagicMock

from apflow.governance.budget import BudgetScope, TokenBudget, BudgetManager


class TestTokenBudget:
    def test_defaults(self):
        b = TokenBudget(scope=BudgetScope.TASK, scope_id="t1", limit=100)
        assert b.remaining == 100
        assert b.utilization == 0.0
        assert not b.is_exhausted

    def test_remaining(self):
        b = TokenBudget(scope=BudgetScope.TASK, scope_id="t1", limit=1000, used=600)
        assert b.remaining == 400

    def test_remaining_over_limit(self):
        b = TokenBudget(scope=BudgetScope.TASK, scope_id="t1", limit=100, used=150)
        assert b.remaining == 0

    def test_utilization(self):
        b = TokenBudget(scope=BudgetScope.TASK, scope_id="t1", limit=1000, used=800)
        assert b.utilization == 0.8

    def test_utilization_over(self):
        b = TokenBudget(scope=BudgetScope.TASK, scope_id="t1", limit=100, used=150)
        assert b.utilization == 1.5

    def test_is_exhausted_at_limit(self):
        b = TokenBudget(scope=BudgetScope.TASK, scope_id="t1", limit=100, used=100)
        assert b.is_exhausted

    def test_is_exhausted_below_limit(self):
        b = TokenBudget(scope=BudgetScope.TASK, scope_id="t1", limit=100, used=99)
        assert not b.is_exhausted

    def test_limit_zero_raises(self):
        with pytest.raises(ValueError):
            TokenBudget(scope=BudgetScope.TASK, scope_id="t1", limit=0)

    def test_negative_used_raises(self):
        with pytest.raises(ValueError):
            TokenBudget(scope=BudgetScope.TASK, scope_id="t1", limit=100, used=-1)

    def test_empty_scope_id_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            TokenBudget(scope=BudgetScope.TASK, scope_id="", limit=100)


def _mock_task(token_budget=None, token_usage=None):
    task = MagicMock()
    task.token_budget = token_budget
    task.token_usage = token_usage
    return task


class TestBudgetManager:
    @pytest.mark.asyncio
    async def test_check_no_budget(self):
        repo = MagicMock()
        repo.get_task_by_id.return_value = _mock_task(token_budget=None)
        bm = BudgetManager(repo)
        result = await bm.check_budget("t1")
        assert result.allowed is True
        assert result.remaining == -1

    @pytest.mark.asyncio
    async def test_check_within_budget(self):
        repo = MagicMock()
        repo.get_task_by_id.return_value = _mock_task(token_budget=1000, token_usage={"total": 500})
        bm = BudgetManager(repo)
        result = await bm.check_budget("t1")
        assert result.allowed is True
        assert result.remaining == 500

    @pytest.mark.asyncio
    async def test_check_exhausted(self):
        repo = MagicMock()
        repo.get_task_by_id.return_value = _mock_task(token_budget=100, token_usage={"total": 100})
        bm = BudgetManager(repo)
        result = await bm.check_budget("t1")
        assert result.allowed is False
        assert result.remaining == 0

    @pytest.mark.asyncio
    async def test_check_empty_id_raises(self):
        bm = BudgetManager(MagicMock())
        with pytest.raises(ValueError):
            await bm.check_budget("")

    @pytest.mark.asyncio
    async def test_check_not_found_raises(self):
        repo = MagicMock()
        repo.get_task_by_id.return_value = None
        bm = BudgetManager(repo)
        with pytest.raises(KeyError):
            await bm.check_budget("nonexistent")

    @pytest.mark.asyncio
    async def test_update_accumulates(self):
        task = _mock_task(
            token_budget=1000, token_usage={"input": 100, "output": 200, "total": 300}
        )
        repo = MagicMock()
        repo.get_task_by_id.return_value = task
        repo.db = MagicMock()
        bm = BudgetManager(repo)

        result = await bm.update_usage("t1", {"input": 50, "output": 50, "total": 100})
        assert result is not None
        assert result.used == 400
        assert task.token_usage["total"] == 400

    @pytest.mark.asyncio
    async def test_update_negative_raises(self):
        bm = BudgetManager(MagicMock())
        with pytest.raises(ValueError, match=">= 0"):
            await bm.update_usage("t1", {"input": -1, "output": 0, "total": -1})

    @pytest.mark.asyncio
    async def test_update_returns_none_no_budget(self):
        task = _mock_task(token_budget=None, token_usage={})
        repo = MagicMock()
        repo.get_task_by_id.return_value = task
        repo.db = MagicMock()
        bm = BudgetManager(repo)
        result = await bm.update_usage("t1", {"input": 100, "output": 100, "total": 200})
        assert result is None
