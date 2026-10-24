"""Missing cost evidence remains visible even in backward compatible numeric reports."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from apflow.core.storage.sqlalchemy.models import TaskModel
from apflow.governance.reporter import UsageReporter


@pytest.mark.asyncio
async def test_usage_report_distinguishes_unknown_cost_from_explicit_zero() -> None:
    unknown = TaskModel(id="unknown", name="unknown", token_usage=None, actual_cost_usd=None)
    free = TaskModel(id="free", name="free", token_usage={"total": 0}, actual_cost_usd=0)
    repository = MagicMock()
    repository.get_task_by_id = AsyncMock(side_effect=[unknown, free])
    reporter = UsageReporter(repository)
    unknown_report = await reporter.get_task_usage("unknown")
    free_report = await reporter.get_task_usage("free")
    assert unknown_report.unknown_cost_task_count == 1
    assert unknown_report.unknown_usage_task_count == 1
    assert free_report.unknown_cost_task_count == 0
    assert free_report.unknown_usage_task_count == 0
    assert json.loads(reporter.export_json(unknown_report))["cost_complete"] is False
    assert json.loads(reporter.export_json(free_report))["cost_complete"] is True
