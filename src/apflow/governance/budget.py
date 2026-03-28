"""
Token budget management for AI agent tasks.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from apflow.logger import get_logger

logger = get_logger(__name__)


class BudgetScope(Enum):
    """Scope of a token budget."""

    TASK = "task"
    USER = "user"


@dataclass
class TokenBudget:
    """Tracks token usage against a budget limit.

    Args:
        scope: Budget scope (TASK or USER).
        scope_id: Identifier for the scope (task_id or user_id).
        limit: Token limit (>= 1).
        used: Tokens consumed so far (>= 0).
    """

    scope: BudgetScope
    scope_id: str
    limit: int
    used: int = 0

    def __post_init__(self) -> None:
        if not self.scope_id:
            raise ValueError("scope_id must be non-empty")
        if self.limit < 1:
            raise ValueError(f"limit must be >= 1, got {self.limit}")
        if self.used < 0:
            raise ValueError(f"used must be >= 0, got {self.used}")

    @property
    def remaining(self) -> int:
        """Tokens remaining (never negative)."""
        return max(0, self.limit - self.used)

    @property
    def utilization(self) -> float:
        """Usage ratio (0.0 to 1.0+). Returns 1.0 if limit is 0."""
        if self.limit == 0:
            return 1.0
        return self.used / self.limit

    @property
    def is_exhausted(self) -> bool:
        """Whether budget is fully consumed."""
        return self.used >= self.limit


@dataclass
class BudgetCheckResult:
    """Result of a budget check."""

    allowed: bool
    remaining: int  # -1 means unlimited
    utilization: float  # 0.0 to 1.0+, -1.0 means no budget


class BudgetManager:
    """Manages token budgets for tasks."""

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def check_budget(self, task_id: str) -> BudgetCheckResult:
        """Check if a task has remaining budget.

        Returns:
            BudgetCheckResult with allowed=True if budget is available or unlimited.
        """
        if not task_id:
            raise ValueError("task_id must be non-empty")

        task = self._repo.get_task_by_id(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")

        if task.token_budget is None:
            return BudgetCheckResult(allowed=True, remaining=-1, utilization=-1.0)

        current_usage = 0
        if task.token_usage and isinstance(task.token_usage, dict):
            current_usage = task.token_usage.get("total", 0)

        budget = TokenBudget(
            scope=BudgetScope.TASK,
            scope_id=task_id,
            limit=task.token_budget,
            used=current_usage,
        )

        return BudgetCheckResult(
            allowed=not budget.is_exhausted,
            remaining=budget.remaining,
            utilization=budget.utilization,
        )

    async def update_usage(
        self, task_id: str, token_usage: Dict[str, int]
    ) -> Optional[TokenBudget]:
        """Update token usage after execution.

        Returns:
            Updated TokenBudget if budget is configured, None otherwise.
        """
        if not task_id:
            raise ValueError("task_id must be non-empty")

        for key in ("input", "output", "total"):
            if key in token_usage and token_usage[key] < 0:
                raise ValueError(f"token_usage['{key}'] must be >= 0, got {token_usage[key]}")

        task = self._repo.get_task_by_id(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")

        # Accumulate usage
        existing = task.token_usage or {}
        accumulated = {
            "input": existing.get("input", 0) + token_usage.get("input", 0),
            "output": existing.get("output", 0) + token_usage.get("output", 0),
            "total": existing.get("total", 0) + token_usage.get("total", 0),
        }

        task.token_usage = accumulated
        self._repo.db.commit()

        if task.token_budget is None:
            return None

        return TokenBudget(
            scope=BudgetScope.TASK,
            scope_id=task_id,
            limit=task.token_budget,
            used=accumulated["total"],
        )
