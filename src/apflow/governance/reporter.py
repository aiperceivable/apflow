"""
Usage reporting for cost governance.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from apflow.logger import get_logger

logger = get_logger(__name__)


@dataclass
class UsageSummary:
    """Aggregated usage summary."""

    scope: str  # "task" or "user"
    scope_id: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    task_count: int = 0
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None


class UsageReporter:
    """Reports token usage and costs."""

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def get_task_usage(self, task_id: str) -> UsageSummary:
        """Get usage summary for a single task."""
        if not task_id:
            raise ValueError("task_id must be non-empty")

        task = self._repo.get_task_by_id(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")

        usage = task.token_usage or {}
        cost = float(task.actual_cost_usd) if task.actual_cost_usd else 0.0

        return UsageSummary(
            scope="task",
            scope_id=task_id,
            total_input_tokens=usage.get("input", 0),
            total_output_tokens=usage.get("output", 0),
            total_tokens=usage.get("total", 0),
            total_cost_usd=cost,
            task_count=1,
        )

    async def get_user_usage(
        self,
        user_id: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> UsageSummary:
        """Get aggregated usage for a user over a time period."""
        if not user_id:
            raise ValueError("user_id must be non-empty")
        if start_time and end_time and end_time < start_time:
            raise ValueError("end_time must be >= start_time")

        tasks = self._repo.list_tasks(user_id=user_id)

        total_input = 0
        total_output = 0
        total_tokens = 0
        total_cost = 0.0
        count = 0

        for task in tasks:
            # Filter by time range if specified
            if start_time and task.created_at and task.created_at < start_time:
                continue
            if end_time and task.created_at and task.created_at > end_time:
                continue

            usage = task.token_usage or {}
            total_input += usage.get("input", 0)
            total_output += usage.get("output", 0)
            total_tokens += usage.get("total", 0)
            if task.actual_cost_usd:
                total_cost += float(task.actual_cost_usd)
            count += 1

        return UsageSummary(
            scope="user",
            scope_id=user_id,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            task_count=count,
            period_start=start_time,
            period_end=end_time,
        )

    def export_json(self, summary: UsageSummary) -> str:
        """Export usage summary as JSON string."""
        data = {
            "scope": summary.scope,
            "scope_id": summary.scope_id,
            "total_input_tokens": summary.total_input_tokens,
            "total_output_tokens": summary.total_output_tokens,
            "total_tokens": summary.total_tokens,
            "total_cost_usd": summary.total_cost_usd,
            "task_count": summary.task_count,
            "period_start": summary.period_start.isoformat() if summary.period_start else None,
            "period_end": summary.period_end.isoformat() if summary.period_end else None,
        }
        return json.dumps(data, indent=2)
