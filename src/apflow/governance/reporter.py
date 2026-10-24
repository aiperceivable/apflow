"""
Usage reporting for cost governance.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from apflow.core.storage.sqlalchemy.models import ExecutionAttempt, TaskModel, UsageEvent

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
    unknown_cost_task_count: int = 0
    unknown_usage_task_count: int = 0
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None

    @property
    def cost_complete(self) -> bool:
        """A numeric aggregate is only complete when every task supplied its actual cost."""
        return self.unknown_cost_task_count == 0


class UsageReporter:
    """Reports token usage and costs."""

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def get_task_usage(self, task_id: str) -> UsageSummary:
        """Get usage summary for a single task."""
        if not task_id:
            raise ValueError("task_id must be non-empty")

        task = await self._repo.get_task_by_id(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")

        usage, cost, unknown_usage, unknown_cost = await self._accounted_usage(task)

        return UsageSummary(
            scope="task",
            scope_id=task_id,
            total_input_tokens=usage.get("input", 0),
            total_output_tokens=usage.get("output", 0),
            total_tokens=usage.get("total", 0),
            total_cost_usd=cost,
            task_count=1,
            unknown_cost_task_count=unknown_cost,
            unknown_usage_task_count=unknown_usage,
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

        total_input = 0
        total_output = 0
        total_tokens = 0
        total_cost = 0.0
        count = 0
        unknown_cost_count = 0
        unknown_usage_count = 0

        # Event time governs managed consumption, including receipts arriving later.
        page_size = 100
        offset = 0
        while True:
            tasks = await self._report_tasks(user_id, start_time, end_time, page_size, offset)
            if not tasks:
                break

            for task in tasks:
                usage, cost, unknown_usage, unknown_cost = await self._accounted_usage(
                    task, start_time, end_time
                )
                total_input += usage.get("input", 0)
                total_output += usage.get("output", 0)
                total_tokens += usage.get("total", 0)
                total_cost += cost
                unknown_cost_count += unknown_cost
                unknown_usage_count += unknown_usage
                count += 1

            if len(tasks) < page_size:
                break
            offset += page_size

        return UsageSummary(
            scope="user",
            scope_id=user_id,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            task_count=count,
            unknown_cost_task_count=unknown_cost_count,
            unknown_usage_task_count=unknown_usage_count,
            period_start=start_time,
            period_end=end_time,
        )

    async def _report_tasks(
        self, user_id: str, start: datetime | None, end: datetime | None, limit: int, offset: int
    ) -> list[TaskModel]:
        session = getattr(getattr(self._repo, "db", None), "session", None)
        if not isinstance(session, (Session, AsyncSession)):
            return await self._repo.query_tasks(
                user_id=user_id, limit=limit, offset=offset, created_after=start, created_before=end
            )
        model = self._repo.task_model_class
        events = select(UsageEvent.id).where(UsageEvent.task_id == model.id)
        window = events
        legacy = []
        if start is not None:
            window = window.where(UsageEvent.created_at >= start)
            legacy.append(model.created_at >= start)
        if end is not None:
            window = window.where(UsageEvent.created_at <= end)
            legacy.append(model.created_at <= end)
        query = select(model).where(
            model.user_id == user_id, or_(window.exists(), and_(~events.exists(), *legacy))
        )
        return list(
            (
                await self._repo.db.execute(query.order_by(model.id).offset(offset).limit(limit))
            ).scalars()
        )

    async def _accounted_usage(
        self, task: TaskModel, start: datetime | None = None, end: datetime | None = None
    ) -> tuple[dict[str, int], float, int, int]:
        """Prefer consumption facts; inherited reference totals never become new spend."""
        session = getattr(getattr(self._repo, "db", None), "session", None)
        events: list[UsageEvent] = []
        attempts: list[ExecutionAttempt] = []
        if isinstance(session, (Session, AsyncSession)):
            events = list(
                (
                    await self._repo.db.execute(
                        select(UsageEvent).where(
                            UsageEvent.task_id == task.id,
                            *([UsageEvent.created_at >= start] if start else []),
                            *([UsageEvent.created_at <= end] if end else []),
                        )
                    )
                ).scalars()
            )
            attempts = list(
                (
                    await self._repo.db.execute(
                        select(ExecutionAttempt).where(ExecutionAttempt.task_id == task.id)
                    )
                ).scalars()
            )
        if task.origin_type in {"link", "copy"} and not events and not attempts:
            return {}, 0.0, int(bool(task.token_usage)), int(task.actual_cost_usd is not None)
        managed = bool(events or attempts or isinstance(task.run_id, str) and task.run_id)
        if managed:
            usage = {"input": 0, "output": 0, "total": 0}
            for event in events:
                for name in usage:
                    usage[name] += event.token_usage.get(name, 0)
            confirmed = {event.invocation_id for event in events if event.evidence is not None}
            unknown = not events or any(
                attempt.id not in confirmed
                and (attempt.status in {"running", "unknown"} or not attempt.token_usage)
                for attempt in attempts
            )
        else:
            usage = task.token_usage or {}
            unknown = not task.token_usage
        cost = float(task.actual_cost_usd) if task.actual_cost_usd else 0.0
        # Legacy row-level money has no per-event timestamp attribution.
        if managed and (start is not None or end is not None):
            return usage, 0.0, int(unknown), 1
        return usage, cost, int(unknown), int(task.actual_cost_usd is None)

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
            "unknown_cost_task_count": summary.unknown_cost_task_count,
            "unknown_usage_task_count": summary.unknown_usage_task_count,
            "cost_complete": summary.cost_complete,
            "period_start": summary.period_start.isoformat() if summary.period_start else None,
            "period_end": summary.period_end.isoformat() if summary.period_end else None,
        }
        return json.dumps(data, indent=2)
