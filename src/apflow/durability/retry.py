"""
Retry logic with configurable backoff strategies.
"""

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, TypeVar

from apflow.core.execution.error_info import ErrorInfo, execution_error_info, validate_error_info
from apflow.core.execution.errors import BusinessError, NetworkError
from apflow.logger import get_logger

logger = get_logger(__name__)
ResultT = TypeVar("ResultT")


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
    retry_unknown_errors: bool = True

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
            # Re-cap after jitter: jitter must not push the delay above the
            # documented backoff_max_seconds ceiling.
            delay = min(delay, self.backoff_max_seconds)

        return max(0.0, delay)


class RetryManager:
    """Manages retry execution with optional checkpoint integration."""

    def __init__(self, checkpoint_manager: Optional[Any] = None) -> None:
        self._checkpoint_manager = checkpoint_manager

    async def execute_with_retry(
        self,
        task_id: str,
        policy: RetryPolicy,
        execute_fn: Callable[..., Awaitable[ResultT]],
        on_retry: Optional[Callable[[str, int, Exception], Awaitable[None]]] = None,
        *,
        cancellation_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> ResultT:
        """Retry classified failures; preserve the final envelope or raise its exception."""
        last_exception: Optional[Exception] = None

        for attempt in range(policy.max_attempts):
            if cancellation_check is not None and await cancellation_check():
                raise asyncio.CancelledError("Task cancellation requested")
            try:
                result = await execute_fn()
                failure = _returned_failure(result)
                if failure is not None:
                    if attempt >= policy.max_attempts - 1 or not _can_retry(failure, policy):
                        return result
                    await self._wait_before_retry(
                        task_id, policy, attempt, failure, on_retry, cancellation_check
                    )
                    continue
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

                if attempt >= policy.max_attempts - 1 or not _can_retry(e, policy):
                    break

                await self._wait_before_retry(
                    task_id, policy, attempt, e, on_retry, cancellation_check
                )

        assert last_exception is not None
        raise last_exception

    async def _wait_before_retry(
        self,
        task_id: str,
        policy: RetryPolicy,
        attempt: int,
        error: Exception,
        on_retry: Optional[Callable[[str, int, Exception], Awaitable[None]]],
        cancellation_check: Callable[[], Awaitable[bool]] | None,
    ) -> None:
        """Keep callback failure separate from execution failure and honor server delay."""
        info = _failure_info(error)
        delay = max(policy.calculate_delay(attempt), (info or {}).get("retry_after_seconds", 0.0))
        error.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)  # type: ignore[attr-defined]
        if on_retry is not None:
            try:
                await on_retry(task_id, attempt, error)
            except Exception as callback_error:
                logger.warning("Task %s: on_retry callback failed: %s", task_id, callback_error)
        logger.debug("Task %s retrying in %.2fs", task_id, delay)
        await _wait_or_cancel(delay, cancellation_check)


class ReturnedExecutionFailure(RuntimeError):
    """Internal retry callback view of a failed envelope, preserving its metadata."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(str(result["error"]))
        self.error_info = result.get("error_info")
        self.token_usage = result.get("token_usage")


def _returned_failure(result: Any) -> ReturnedExecutionFailure | None:
    if isinstance(result, dict) and result.get("error"):
        return ReturnedExecutionFailure(result)
    return None


def _failure_info(error: Exception) -> ErrorInfo | None:
    info = getattr(error, "error_info", None)
    return validate_error_info(info) if info else None


def _can_retry(error: Exception, policy: RetryPolicy) -> bool:
    if isinstance(error, (ValueError, TypeError, PermissionError)):
        return False
    if isinstance(error, BusinessError) and not isinstance(error, NetworkError):
        return False
    info = _failure_info(error)
    if info is not None:
        return (
            info["retryable"] and info.get("retry_after_seconds", 0) <= policy.backoff_max_seconds
        )
    if execution_error_info(error)["category"] == "permanent":
        return False
    if isinstance(error, (ReturnedExecutionFailure, NetworkError)):
        return False
    return policy.retry_unknown_errors


async def _wait_or_cancel(
    delay: float, cancellation_check: Callable[[], Awaitable[bool]] | None
) -> None:
    if cancellation_check is None:
        await asyncio.sleep(delay)
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while True:
        if await cancellation_check():
            raise asyncio.CancelledError("Task cancellation requested")
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 0.1))
