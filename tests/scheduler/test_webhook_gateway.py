"""Tests for WebhookGateway._execute_task (apflow.scheduler.gateway.webhook)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apflow.scheduler.gateway.webhook import WebhookGateway


@asynccontextmanager
async def _fake_session():
    yield MagicMock()


@pytest.mark.asyncio
async def test_execute_task_surfaces_schedule_advance_failure(caplog) -> None:
    """Regression: complete_scheduled_run's return value was discarded
    entirely. When it returns None (an internal failure to advance the
    schedule, e.g. a corrupt stored schedule), the task's own execution
    result silently masked it — the task could remain "due" for an external
    poller to re-trigger despite having already run once, undermining
    effectively-once dispatch. (Review CRITICAL #12)
    """
    definition = SimpleNamespace(id="sched1")
    run_instance = SimpleNamespace(
        id="run1", status="completed", result={"ok": True}, error=None, has_children=False
    )
    run_tree = SimpleNamespace(task=SimpleNamespace(id="run1"), children=[])

    repo = AsyncMock()
    repo.get_task_by_id.side_effect = [definition, run_instance, run_instance]
    repo.get_task_tree_for_api.return_value = run_tree
    repo.complete_scheduled_run.return_value = None  # schedule failed to advance

    creator = MagicMock()
    creator.instantiate_scheduled_run = AsyncMock(return_value=run_tree)

    executor = MagicMock()
    executor.execute_task_tree = AsyncMock(return_value=None)

    with (
        patch("apflow.core.storage.create_pooled_session", side_effect=_fake_session),
        patch(
            "apflow.core.storage.sqlalchemy.task_repository.TaskRepository",
            return_value=repo,
        ),
        patch("apflow.core.execution.task_creator.TaskCreator", return_value=creator),
        patch("apflow.core.execution.task_executor.TaskExecutor", return_value=executor),
    ):
        gateway = WebhookGateway()
        with caplog.at_level(logging.ERROR):
            result = await gateway._execute_task("sched1")

    # The task run itself succeeded ...
    assert result["success"] is True
    # ... but the schedule failing to advance must be surfaced, not swallowed.
    assert result["schedule_advanced"] is False
    assert any("schedule" in record.message.lower() for record in caplog.records)


@pytest.mark.asyncio
async def test_execute_task_reports_schedule_advanced_on_success() -> None:
    definition = SimpleNamespace(id="sched1")
    run_instance = SimpleNamespace(
        id="run1", status="completed", result={"ok": True}, error=None, has_children=False
    )
    run_tree = SimpleNamespace(task=SimpleNamespace(id="run1"), children=[])

    repo = AsyncMock()
    repo.get_task_by_id.side_effect = [definition, run_instance, run_instance]
    repo.get_task_tree_for_api.return_value = run_tree
    repo.complete_scheduled_run.return_value = definition  # advanced successfully

    creator = MagicMock()
    creator.instantiate_scheduled_run = AsyncMock(return_value=run_tree)

    executor = MagicMock()
    executor.execute_task_tree = AsyncMock(return_value=None)

    with (
        patch("apflow.core.storage.create_pooled_session", side_effect=_fake_session),
        patch(
            "apflow.core.storage.sqlalchemy.task_repository.TaskRepository",
            return_value=repo,
        ),
        patch("apflow.core.execution.task_creator.TaskCreator", return_value=creator),
        patch("apflow.core.execution.task_executor.TaskExecutor", return_value=executor),
    ):
        gateway = WebhookGateway()
        result = await gateway._execute_task("sched1")

    assert result["success"] is True
    assert result["schedule_advanced"] is True
