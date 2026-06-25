"""Tests for the schedule.* apcore modules (apflow.bridge.schedule_modules)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from apflow.bridge.schedule_modules import (
    ScheduleCompleteModule,
    ScheduleDueModule,
    ScheduleExportICalModule,
    ScheduleSetModule,
)


def _task(**overrides: Any) -> SimpleNamespace:
    base = {
        "id": "t1",
        "name": "job",
        "schedule_type": "cron",
        "schedule_expression": "0 9 * * *",
        "next_run_at": "2026-06-26T09:00:00",
    }
    base.update(overrides)
    task = SimpleNamespace(**base)
    task.to_dict = lambda: dict(base)
    return task


@pytest.mark.asyncio
async def test_schedule_set_updates_then_initializes() -> None:
    repo = AsyncMock()
    repo.initialize_schedule.return_value = _task()
    out = await ScheduleSetModule(repo).execute(
        {
            "task_id": "t1",
            "schedule_type": "cron",
            "schedule_expression": "0 9 * * *",
            "max_runs": "5",
        }
    )
    kwargs = repo.update_task.await_args.kwargs
    assert kwargs["task_id"] == "t1"
    assert kwargs["schedule_type"] == "cron"
    assert kwargs["schedule_enabled"] is True
    assert kwargs["max_runs"] == 5  # coerced from "5"
    repo.initialize_schedule.assert_awaited_once_with("t1")
    assert out["id"] == "t1"


@pytest.mark.asyncio
async def test_schedule_set_requires_type_and_expression() -> None:
    with pytest.raises(ValueError):
        await ScheduleSetModule(AsyncMock()).execute({"task_id": "t1"})


@pytest.mark.asyncio
async def test_schedule_set_missing_task_raises_keyerror() -> None:
    repo = AsyncMock()
    repo.initialize_schedule.return_value = None
    with pytest.raises(KeyError):
        await ScheduleSetModule(repo).execute(
            {"task_id": "x", "schedule_type": "cron", "schedule_expression": "* * * * *"}
        )


@pytest.mark.asyncio
async def test_schedule_due_lists_and_clamps_limit() -> None:
    repo = AsyncMock()
    repo.get_due_scheduled_tasks.return_value = [_task(), _task(id="t2")]
    out = await ScheduleDueModule(repo).execute({"limit": "9999"})
    assert repo.get_due_scheduled_tasks.await_args.kwargs["limit"] == 1000  # clamped to max
    assert out["count"] == 2
    assert out["tasks"][0]["id"] == "t1"


@pytest.mark.asyncio
async def test_schedule_complete_coerces_and_calls_repo() -> None:
    repo = AsyncMock()
    repo.complete_scheduled_run.return_value = _task()
    out = await ScheduleCompleteModule(repo).execute(
        {"task_id": "t1", "success": False, "error": "boom"}
    )
    kwargs = repo.complete_scheduled_run.await_args.kwargs
    assert kwargs["task_id"] == "t1"
    assert kwargs["success"] is False
    assert kwargs["error"] == "boom"
    assert out["id"] == "t1"


@pytest.mark.asyncio
async def test_schedule_export_ical_returns_feed() -> None:
    with patch("apflow.scheduler.gateway.ical.ICalExporter") as exporter_cls:
        exporter_cls.return_value.export_tasks = AsyncMock(
            return_value="BEGIN:VCALENDAR\nEND:VCALENDAR"
        )
        out = await ScheduleExportICalModule().execute({"limit": 10})
    assert out["ical"].startswith("BEGIN:VCALENDAR")
    assert out["format"] == "text/calendar"
