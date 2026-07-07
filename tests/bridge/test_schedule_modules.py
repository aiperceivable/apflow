"""Tests for the schedule.* apcore modules (apflow.bridge.schedule_modules)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from apflow.bridge.schedule_modules import (
    ScheduleCompleteModule,
    ScheduleDueModule,
    ScheduleExportICalModule,
    ScheduleHistoryModule,
    ScheduleSetModule,
    ScheduleTriggerModule,
)

# ScheduleSetModule validates the schedule via ScheduleCalculator before writing;
# patch it so tests don't depend on real cron/interval parsing.
_CALC = "apflow.core.storage.sqlalchemy.schedule_calculator.ScheduleCalculator.calculate_next_run"


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


def _run(**overrides: Any) -> SimpleNamespace:
    base = {
        "id": "run1",
        "status": "completed",
        "result": {"ok": True},
        "error": None,
        "token_usage": {"total_tokens": 42},
        "created_at": "2026-07-06T09:00:00",
        "started_at": "2026-07-06T09:00:01",
        "completed_at": "2026-07-06T09:00:02",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_schedule_set_updates_then_initializes() -> None:
    repo = AsyncMock()
    repo.get_task_by_id.return_value = _task()
    repo.initialize_schedule.return_value = _task()
    with patch(_CALC, return_value=None):
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
async def test_schedule_set_invalid_schedule_rejected_without_write() -> None:
    # An invalid schedule must surface a ValueError AND leave the task untouched
    # (no partial write) — not a misleading "task not found".
    repo = AsyncMock()
    with patch(_CALC, side_effect=ValueError("Unknown schedule type: bogus")):
        with pytest.raises(ValueError, match="Invalid schedule"):
            await ScheduleSetModule(repo).execute(
                {"task_id": "t1", "schedule_type": "bogus", "schedule_expression": "x"}
            )
    repo.update_task.assert_not_awaited()
    repo.initialize_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_set_missing_task_raises_keyerror() -> None:
    repo = AsyncMock()
    repo.get_task_by_id.return_value = None  # task does not exist
    with patch(_CALC, return_value=None):
        with pytest.raises(KeyError):
            await ScheduleSetModule(repo).execute(
                {"task_id": "x", "schedule_type": "cron", "schedule_expression": "* * * * *"}
            )
    repo.update_task.assert_not_awaited()  # no write for a missing task


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
    repo.get_task_by_id.return_value = _task()
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
async def test_schedule_complete_missing_task_raises_keyerror() -> None:
    repo = AsyncMock()
    repo.get_task_by_id.return_value = None
    with pytest.raises(KeyError):
        await ScheduleCompleteModule(repo).execute({"task_id": "x"})
    repo.complete_scheduled_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_complete_existing_task_op_failure_is_not_keyerror() -> None:
    # An existing task whose completion fails (e.g. corrupt stored schedule) must NOT
    # be reported as "not found" (KeyError → REST 404).
    repo = AsyncMock()
    repo.get_task_by_id.return_value = _task()
    repo.complete_scheduled_run.return_value = None  # op failed for an existing task
    with pytest.raises(RuntimeError):
        await ScheduleCompleteModule(repo).execute({"task_id": "t1"})


@pytest.mark.asyncio
async def test_schedule_history_lists_runs_newest_first() -> None:
    repo = AsyncMock()
    repo.get_task_by_id.return_value = _task()
    repo.list_scheduled_runs.return_value = [_run(), _run(id="run2")]
    out = await ScheduleHistoryModule(repo).execute({"task_id": "t1"})
    repo.list_scheduled_runs.assert_awaited_once_with("t1", limit=100, offset=0)
    assert out["count"] == 2
    assert [r["id"] for r in out["runs"]] == ["run1", "run2"]
    assert out["runs"][0]["result"] == {"ok": True}
    assert out["runs"][0]["token_usage"] == {"total_tokens": 42}


@pytest.mark.asyncio
async def test_schedule_history_clamps_limit_and_offset() -> None:
    repo = AsyncMock()
    repo.get_task_by_id.return_value = _task()
    repo.list_scheduled_runs.return_value = []
    await ScheduleHistoryModule(repo).execute({"task_id": "t1", "limit": "9999", "offset": "-5"})
    kwargs = repo.list_scheduled_runs.await_args.kwargs
    assert kwargs["limit"] == 1000  # clamped to max
    assert kwargs["offset"] == 0  # clamped to min


@pytest.mark.asyncio
async def test_schedule_history_missing_task_raises_keyerror() -> None:
    repo = AsyncMock()
    repo.get_task_by_id.return_value = None
    with pytest.raises(KeyError):
        await ScheduleHistoryModule(repo).execute({"task_id": "x"})
    repo.list_scheduled_runs.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_history_requires_task_id() -> None:
    with pytest.raises(ValueError):
        await ScheduleHistoryModule(AsyncMock()).execute({"task_id": ""})


@pytest.mark.asyncio
async def test_schedule_trigger_executes_via_gateway() -> None:
    # An existing task is handed to WebhookGateway.trigger_task, whose result
    # (success/status/result) is returned verbatim, and async_execution is threaded.
    repo = AsyncMock()
    repo.get_task_by_id.return_value = _task()
    gateway_result = {"success": True, "status": "completed", "task_id": "t1"}
    with patch("apflow.scheduler.gateway.webhook.WebhookGateway") as gateway_cls:
        gateway_cls.return_value.trigger_task = AsyncMock(return_value=gateway_result)
        out = await ScheduleTriggerModule(repo).execute(
            {"task_id": "t1", "user_id": "u1", "async_execution": True}
        )
    call = gateway_cls.return_value.trigger_task.await_args
    assert call is not None
    assert call.args == ("t1",)
    assert call.kwargs["user_id"] == "u1"
    assert call.kwargs["execute_async"] is True
    assert out == gateway_result


@pytest.mark.asyncio
async def test_schedule_trigger_missing_task_raises_keyerror() -> None:
    # A genuine miss is a KeyError (REST 404), distinct from a task whose own
    # execution fails (which is a valid success=False gateway result).
    repo = AsyncMock()
    repo.get_task_by_id.return_value = None
    with patch("apflow.scheduler.gateway.webhook.WebhookGateway") as gateway_cls:
        with pytest.raises(KeyError):
            await ScheduleTriggerModule(repo).execute({"task_id": "x"})
    gateway_cls.return_value.trigger_task.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_trigger_requires_task_id() -> None:
    with pytest.raises(ValueError):
        await ScheduleTriggerModule(AsyncMock()).execute({"task_id": ""})


@pytest.mark.asyncio
async def test_schedule_export_ical_returns_feed() -> None:
    with patch("apflow.scheduler.gateway.ical.ICalExporter") as exporter_cls:
        exporter_cls.return_value.export_tasks = AsyncMock(
            return_value="BEGIN:VCALENDAR\nEND:VCALENDAR"
        )
        out = await ScheduleExportICalModule().execute({"limit": 10})
    assert out["ical"].startswith("BEGIN:VCALENDAR")
    assert out["format"] == "text/calendar"
