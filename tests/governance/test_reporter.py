"""Tests for reporter module"""

import json
from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock

from apflow.governance.reporter import UsageReporter, UsageSummary


def _mock_task(token_usage=None, actual_cost_usd=None, created_at=None):
    task = MagicMock()
    task.token_usage = token_usage
    task.actual_cost_usd = actual_cost_usd
    task.created_at = created_at
    return task


class TestGetTaskUsage:
    @pytest.mark.asyncio
    async def test_returns_usage(self):
        """Regression: get_task_usage never awaited get_task_by_id, so it
        crashed with AttributeError against a real (async) TaskRepository —
        masked here previously by a plain MagicMock that returned the task
        synchronously. (Review CRITICAL #64)"""
        repo = MagicMock()
        repo.get_task_by_id = AsyncMock(
            return_value=_mock_task(
                token_usage={"input": 100, "output": 200, "total": 300},
                actual_cost_usd=0.015,
            )
        )
        reporter = UsageReporter(repo)
        result = await reporter.get_task_usage("t1")
        assert result.total_tokens == 300
        assert result.total_input_tokens == 100
        assert result.total_output_tokens == 200
        assert result.total_cost_usd == 0.015

    @pytest.mark.asyncio
    async def test_no_usage_returns_zeros(self):
        repo = MagicMock()
        repo.get_task_by_id = AsyncMock(return_value=_mock_task())
        reporter = UsageReporter(repo)
        result = await reporter.get_task_usage("t1")
        assert result.total_tokens == 0
        assert result.total_cost_usd == 0.0

    @pytest.mark.asyncio
    async def test_not_found_raises(self):
        repo = MagicMock()
        repo.get_task_by_id = AsyncMock(return_value=None)
        with pytest.raises(KeyError):
            await UsageReporter(repo).get_task_usage("nonexistent")

    @pytest.mark.asyncio
    async def test_empty_id_raises(self):
        with pytest.raises(ValueError):
            await UsageReporter(MagicMock()).get_task_usage("")


class TestGetUserUsage:
    @pytest.mark.asyncio
    async def test_aggregation(self):
        """Regression: get_user_usage called the nonexistent
        TaskRepository.list_tasks(), unawaited. The real method is
        query_tasks(). (Review CRITICAL #65)"""
        repo = MagicMock()
        repo.query_tasks = AsyncMock(
            side_effect=[
                [
                    _mock_task(
                        token_usage={"input": 50, "output": 50, "total": 100},
                        actual_cost_usd=0.01,
                    ),
                    _mock_task(
                        token_usage={"input": 100, "output": 100, "total": 200},
                        actual_cost_usd=0.02,
                    ),
                    _mock_task(
                        token_usage={"input": 150, "output": 150, "total": 300},
                        actual_cost_usd=0.03,
                    ),
                ],
            ]
        )
        reporter = UsageReporter(repo)
        result = await reporter.get_user_usage("u1")
        assert result.total_tokens == 600
        assert result.task_count == 3
        assert result.total_cost_usd == pytest.approx(0.06)

    @pytest.mark.asyncio
    async def test_pages_through_all_results(self):
        """Regression guard: query_tasks() is paginated (default limit=100).
        A user with more tasks than one page must not be silently truncated
        to only the first page."""
        page_size = 100
        first_page = [
            _mock_task(token_usage={"input": 1, "output": 1, "total": 2}, actual_cost_usd=0.001)
            for _ in range(page_size)
        ]
        second_page = [
            _mock_task(token_usage={"input": 1, "output": 1, "total": 2}, actual_cost_usd=0.001)
            for _ in range(5)
        ]
        repo = MagicMock()
        repo.query_tasks = AsyncMock(side_effect=[first_page, second_page])

        result = await UsageReporter(repo).get_user_usage("u1")

        assert result.task_count == page_size + 5
        assert result.total_tokens == (page_size + 5) * 2
        assert repo.query_tasks.await_count == 2

    @pytest.mark.asyncio
    async def test_empty_returns_zeros(self):
        repo = MagicMock()
        repo.query_tasks = AsyncMock(return_value=[])
        result = await UsageReporter(repo).get_user_usage("u1")
        assert result.total_tokens == 0
        assert result.task_count == 0

    @pytest.mark.asyncio
    async def test_invalid_period_raises(self):
        start = datetime(2026, 3, 28, tzinfo=timezone.utc)
        end = datetime(2026, 3, 27, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="start_time"):
            await UsageReporter(MagicMock()).get_user_usage("u1", start, end)

    @pytest.mark.asyncio
    async def test_empty_user_id_raises(self):
        with pytest.raises(ValueError):
            await UsageReporter(MagicMock()).get_user_usage("")


class TestExportJson:
    def test_format(self):
        summary = UsageSummary(
            scope="task",
            scope_id="t1",
            total_input_tokens=100,
            total_output_tokens=200,
            total_tokens=300,
            total_cost_usd=0.015,
            task_count=1,
        )
        json_str = UsageReporter(MagicMock()).export_json(summary)
        data = json.loads(json_str)
        assert data["total_tokens"] == 300
        assert data["total_cost_usd"] == 0.015
        assert data["scope"] == "task"
        assert data["scope_id"] == "t1"
