"""Retry only classified execution failures, preserving business rejections."""

from unittest.mock import AsyncMock, patch
import asyncio

import pytest

from apflow.core.execution.errors import (
    AuthenticationError,
    ConfigurationError,
    ValidationError,
    execution_error_info,
    http_error_info,
)
from apflow.durability.retry import RetryManager, RetryPolicy
from apcore.errors import ModuleError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ValidationError("bad"),
        ConfigurationError("bad"),
        AuthenticationError("bad"),
        ValueError("bad"),
        ModuleError(code="ACL_DENIED", message="Denied"),
    ],
)
async def test_permanent_errors_do_not_retry(error: Exception) -> None:
    execute = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await RetryManager().execute_with_retry("task", RetryPolicy(max_attempts=3), execute)
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_transient_http_envelope_retries_and_honors_retry_after() -> None:
    failure = {
        "result": {"status_code": 503},
        "error": "HTTP request failed",
        "error_info": http_error_info(503, "GET", retry_after="2"),
    }
    execute = AsyncMock(side_effect=[failure, {"result": "ok"}])
    with patch("apflow.durability.retry.asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await RetryManager().execute_with_retry(
            "task", RetryPolicy(max_attempts=3, jitter=False), execute
        )
    assert result == {"result": "ok"}
    sleep.assert_awaited_once_with(2.0)


@pytest.mark.asyncio
async def test_unsafe_post_response_and_business_rejection_do_not_retry() -> None:
    for result in (
        {"result": None, "error": "Unavailable", "error_info": http_error_info(503, "POST")},
        {"result": {"verdict": "rejected"}},
    ):
        execute = AsyncMock(return_value=result)
        assert await RetryManager().execute_with_retry("task", RetryPolicy(), execute) == result
        assert execute.await_count == 1


@pytest.mark.asyncio
async def test_unknown_error_is_not_replayed_in_safe_task_mode() -> None:
    execute = AsyncMock(side_effect=RuntimeError("response lost"))
    with pytest.raises(RuntimeError):
        await RetryManager().execute_with_retry(
            "task", RetryPolicy(retry_unknown_errors=False), execute
        )
    assert execute.await_count == 1
    assert execution_error_info(RuntimeError("failure"))["delivery_state"] == "unknown"


@pytest.mark.asyncio
async def test_exhausted_envelope_preserves_final_error_and_partial_result() -> None:
    result = {
        "result": {"partial": True},
        "error": "Unavailable",
        "error_info": http_error_info(503, "GET"),
    }
    execute = AsyncMock(return_value=result)
    with patch("apflow.durability.retry.asyncio.sleep", new_callable=AsyncMock):
        assert (
            await RetryManager().execute_with_retry("task", RetryPolicy(max_attempts=2), execute)
            == result
        )
    assert execute.await_count == 2


@pytest.mark.asyncio
async def test_cancel_request_interrupts_retry_after_wait() -> None:
    failure = {
        "result": None,
        "error": "Rate limited",
        "error_info": http_error_info(429, "GET", retry_after="10"),
    }
    execute = AsyncMock(return_value=failure)
    cancellation = AsyncMock(side_effect=[False, True])
    with pytest.raises(asyncio.CancelledError):
        await RetryManager().execute_with_retry(
            "task", RetryPolicy(), execute, cancellation_check=cancellation
        )
    assert execute.await_count == 1
