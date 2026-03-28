"""
Retry logic with configurable backoff strategies.
"""

import asyncio
import random
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional

from apflow.logger import get_logger

logger = get_logger(__name__)


class BackoffStrategy(Enum):
    """Backoff strategy for retries."""

    FIXED = "fixed"
    EXPONENTIAL = "exponential"
    LINEAR = "linear"


@dataclass(frozen=True)
class RetryPolicy:
    """Configuration for retry behavior.

    Args:
        max_attempts: Total attempts (1 = no retry). Must be 1-100.
        backoff_strategy: How delay increases between retries.
        backoff_base_seconds: Base delay in seconds. Must be 0.1-3600.0.
        backoff_max_seconds: Max delay cap. Must be >= base, <= 86400.0.
        jitter: Add random +-25% to delay to avoid thundering herd.
    """

    max_attempts: int = 3
    backoff_strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 300.0
    jitter: bool = True

    def __post_init__(self) -> None:
        if not (1 <= self.max_attempts <= 100):
            raise ValueError(f"max_attempts must be 1-100, got {self.max_attempts}")
        if not (0.1 <= self.backoff_base_seconds <= 3600.0):
            raise ValueError(
                f"backoff_base_seconds must be 0.1-3600.0, got {self.backoff_base_seconds}"
            )
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError(
                f"backoff_max_seconds ({self.backoff_max_seconds}) "
                f"must be >= backoff_base_seconds ({self.backoff_base_seconds})"
            )
        if self.backoff_max_seconds > 86400.0:
            raise ValueError(
                f"backoff_max_seconds must be <= 86400.0, got {self.backoff_max_seconds}"
            )

    def calculate_delay(self, attempt: int) -> float:
        """Calculate delay for a given retry attempt (0-indexed).

        Returns:
            Delay in seconds, capped at backoff_max_seconds.
        """
        if attempt < 0:
            raise ValueError(f"attempt must be >= 0, got {attempt}")

        if self.backoff_strategy == BackoffStrategy.FIXED:
            delay = self.backoff_base_seconds
        elif self.backoff_strategy == BackoffStrategy.EXPONENTIAL:
            delay = self.backoff_base_seconds * (2**attempt)
        elif self.backoff_strategy == BackoffStrategy.LINEAR:
            delay = self.backoff_base_seconds * (attempt + 1)
        else:
            delay = self.backoff_base_seconds

        delay = min(delay, self.backoff_max_seconds)

        if self.jitter:
            jitter_range = delay * 0.25
            delay += random.uniform(-jitter_range, jitter_range)

        return max(0.0, delay)


class RetryManager:
    """Manages retry execution with optional checkpoint integration."""

    def __init__(self, checkpoint_manager: Optional[Any] = None) -> None:
        self._checkpoint_manager = checkpoint_manager

    async def execute_with_retry(
        self,
        task_id: str,
        policy: RetryPolicy,
        execute_fn: Callable[..., Awaitable[Dict[str, Any]]],
        on_retry: Optional[Callable[[str, int, Exception], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """Execute a function with retry logic.

        Args:
            task_id: Task identifier for logging and checkpointing.
            policy: Retry policy configuration.
            execute_fn: Async function to execute.
            on_retry: Optional callback called before each retry.

        Returns:
            Result from execute_fn.

        Raises:
            The last exception if all attempts fail.
        """
        last_exception: Optional[Exception] = None

        for attempt in range(policy.max_attempts):
            try:
                result = await execute_fn()
                if attempt > 0:
                    logger.info(
                        f"Task {task_id} succeeded on attempt {attempt + 1}/{policy.max_attempts}"
                    )
                return result
            except Exception as e:
                last_exception = e
                logger.warning(
                    f"Task {task_id} attempt {attempt + 1}/{policy.max_attempts} failed: {e}"
                )

                if attempt >= policy.max_attempts - 1:
                    break

                if on_retry is not None:
                    await on_retry(task_id, attempt, e)

                delay = policy.calculate_delay(attempt)
                logger.debug(f"Task {task_id} retrying in {delay:.2f}s")
                await asyncio.sleep(delay)

        raise last_exception  # type: ignore[misc]
