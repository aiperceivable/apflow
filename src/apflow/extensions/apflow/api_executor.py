"""
apflow API Executor for calling other apflow instances

This executor allows tasks to call other apflow API services
for distributed execution, service orchestration, and load balancing.

Production-Grade Polling Implementation:
========================================

Why Infinite Loops Occur:
-------------------------
1. **Network Failures**: Transient network issues (timeouts, connection errors) can cause
   continuous polling failures without proper backoff
2. **Server Errors**: 5xx errors from the API server can persist, causing repeated failures
3. **Invalid Responses**: Malformed JSON or missing fields can cause parsing errors repeatedly
4. **Test Environment**: In tests, improperly mocked responses can cause infinite loops

Production Solutions Implemented:
---------------------------------
1. **Exponential Backoff**: Wait time increases exponentially on failures (1s, 2s, 4s, 8s...)
   - Reduces load on failing services
   - Gives services time to recover
   - Capped at max_backoff (30s) to prevent excessive waits

2. **Circuit Breaker Pattern**: Stops polling after max_consecutive_failures (10)
   - Prevents resource waste on persistent failures
   - Fast failure for unrecoverable errors
   - Allows system to fail fast and report errors

3. **Error Classification**:
   - **Retryable**: Network errors, 5xx server errors, timeouts
   - **Non-retryable**: 4xx client errors (invalid task_id, auth issues)
   - **Fast-fail**: 4xx errors fail after 3 consecutive attempts

4. **Total Failure Threshold**: Stops after max_total_failures (20) across all polls
   - Prevents infinite loops even with intermittent successes
   - Accounts for partial recovery scenarios

5. **Detailed Logging**: Comprehensive logging for debugging
   - Poll count, failure counts, error types
   - Helps identify patterns in production issues

6. **Timeout Protection**: Always respects overall timeout
   - Prevents infinite loops even if other safeguards fail
   - Ensures predictable behavior

Configuration:
--------------
- poll_interval: Base polling interval (default: 1.0s)
- timeout: Total timeout (default: 300s)
- max_consecutive_failures: Circuit breaker threshold (10)
- max_total_failures: Total failure threshold (20)
- max_backoff: Maximum backoff time (30s)
"""

import asyncio
import httpx
from typing import Any, ClassVar, Dict, Literal, Optional

from pydantic import BaseModel, Field
from apflow.core.base import BaseTask
from apflow.core.extensions.decorators import executor_register
from apflow.core.utils.network_security import validate_url_not_private
from apflow.logger import get_logger

logger = get_logger(__name__)


class ApFlowApiInputSchema(BaseModel):
    base_url: str = Field(
        description='apflow API base URL (e.g., "http://localhost:8000") (required)'
    )
    method: str = Field(
        description='API method name (e.g., "tasks.execute", "tasks.create") (required)'
    )
    params: Dict[str, Any] = Field(description="Method parameters dict (required)")
    auth_token: Optional[str] = Field(
        default=None, description="Optional JWT token for authentication"
    )
    use_streaming: bool = Field(
        default=False,
        description="Not implemented; setting this to True raises an error. "
        "Use wait_for_completion instead.",
    )
    wait_for_completion: bool = Field(
        default=False,
        description="Whether to wait for task completion (only for tasks.execute, default: False)",
    )
    poll_interval: float = Field(
        default=1.0,
        description="Polling interval in seconds when waiting for completion (default: 1.0)",
    )
    timeout: float = Field(default=300.0, description="Total timeout in seconds (default: 300.0)")
    headers: Optional[Dict[str, str]] = Field(
        default=None, description="Additional HTTP headers dict (optional)"
    )


class ApFlowApiOutputSchema(BaseModel):
    success: bool = Field(description="Whether the API call was successful")
    result: Optional[Dict[str, Any]] = Field(
        default=None, description="API response result data (only present on success)"
    )
    error: Optional[str] = Field(
        default=None, description="Error message (only present on failure)"
    )
    error_code: Optional[int] = Field(
        default=None, description="JSON-RPC error code (optional, only present for JSON-RPC errors)"
    )
    error_data: Optional[Dict[str, Any]] = Field(
        default=None, description="JSON-RPC error data (optional, only present for JSON-RPC errors)"
    )
    status_code: Optional[int] = Field(
        default=None, description="HTTP status code (optional, only present for HTTP errors)"
    )
    response: Optional[str] = Field(
        default=None, description="Raw HTTP response text (optional, only present for HTTP errors)"
    )
    base_url: str = Field(description="The apflow API base URL that was called")
    method: str = Field(description="The API method that was called")
    task_id: Optional[str] = Field(
        default=None, description="Task ID (only present when waiting for task completion)"
    )
    status: Optional[str] = Field(
        default=None,
        description="Final task status (only present when waiting for task completion)",
    )
    task: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Complete task data (only present when waiting for task completion)",
    )
    poll_count: Optional[int] = Field(
        default=None,
        description="Number of polling attempts (only present when waiting for task completion)",
    )
    total_failures: Optional[int] = Field(
        default=None,
        description="Total polling failures (only present when waiting for task completion)",
    )
    consecutive_failures: Optional[int] = Field(
        default=None,
        description="Consecutive polling failures (only present in circuit breaker scenarios)",
    )
    error_type: Optional[Literal["client_error", "circuit_breaker", "max_failures"]] = Field(
        default=None, description="Type of polling error (only present in specific error scenarios)"
    )


@executor_register()
class ApFlowApiExecutor(BaseTask):
    """
    Executor for calling other apflow API instances

    Supports all task management methods (tasks.execute, tasks.create, tasks.get, etc.)
    with authentication and polling-based result retrieval (use_streaming is not
    implemented).

    Example usage in task schemas:
    {
        "schemas": {
            "method": "apflow_api_executor"  # Executor id
        },
        "inputs": {
            "base_url": "http://remote-instance:8000",
            "method": "tasks.execute",
            "params": {"task_id": "task-123"},
            "auth_token": "eyJ...",
            "wait_for_completion": true
        }
    }
    """

    id = "apflow_api_executor"
    name = "apflow API Executor"
    description = "Call other apflow API instances for distributed execution"
    tags = ["apflow", "api", "distributed", "orchestration"]
    examples = [
        "Call remote apflow instance",
        "Distributed task execution",
        "Service orchestration across instances",
    ]

    # Cancellation support: Can be cancelled by stopping HTTP request
    cancelable: bool = True

    inputs_schema: ClassVar[type[BaseModel]] = ApFlowApiInputSchema
    outputs_schema: ClassVar[type[BaseModel]] = ApFlowApiOutputSchema

    @property
    def type(self) -> str:
        """Extension type identifier for categorization"""
        return "apflow"

    async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute an apflow API call

        Args:
            inputs: Dictionary containing:
                - base_url: apflow API base URL (e.g., "http://localhost:8000") (required)
                - method: API method name (e.g., "tasks.execute", "tasks.create") (required)
                - params: Method parameters dict (required)
                - auth_token: Optional JWT token for authentication
                - use_streaming: Not implemented; raises if set to True
                - wait_for_completion: Whether to wait for task completion (only for tasks.execute, default: False)
                - poll_interval: Polling interval in seconds when waiting for completion (default: 1.0)
                - timeout: Total timeout in seconds (default: 300.0)
                - headers: Additional HTTP headers dict (optional)

        Returns:
            Dictionary with API response data
        """
        base_url = inputs.get("base_url")
        if not base_url:
            raise ValueError("base_url is required in inputs")

        method = inputs.get("method")
        if not method:
            raise ValueError("method is required in inputs")

        params = inputs.get("params", {})
        auth_token = inputs.get("auth_token")
        if inputs.get("use_streaming", False):
            # Not implemented: there is no streaming code path in this executor.
            # Silently ignoring the flag would make the caller believe streaming
            # happened when it never did — fail loudly instead.
            raise ValueError(
                f"[{self.id}] use_streaming is not implemented; "
                "use wait_for_completion for polling-based result retrieval instead"
            )
        wait_for_completion = inputs.get("wait_for_completion", False)
        poll_interval = inputs.get("poll_interval", 1.0)
        timeout = inputs.get("timeout", 300.0)
        headers = inputs.get("headers", {})

        # Prepare headers
        request_headers = {"Content-Type": "application/json", **headers}

        if auth_token:
            request_headers["Authorization"] = f"Bearer {auth_token}"

        # Prepare JSON-RPC request
        jsonrpc_request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": f"apflow_executor_{asyncio.get_event_loop().time()}",
        }

        # Construct API endpoint URL
        api_url = f"{base_url.rstrip('/')}/tasks"

        logger.info(f"Calling apflow API {method} on {base_url}")

        try:
            # base_url is task-supplied; without this check a task could target
            # internal/private infrastructure (e.g. the cloud metadata endpoint)
            # via this executor (unlike sibling rest_executor, which already
            # validates outbound URLs the same way).
            await validate_url_not_private(api_url, self.id)

            async with httpx.AsyncClient(timeout=timeout) as client:
                # Check for cancellation before making request
                if self.cancellation_checker and self.cancellation_checker():
                    logger.info("apflow API call cancelled before execution")
                    return {
                        "success": False,
                        "error": "Call was cancelled",
                        "base_url": base_url,
                        "method": method,
                    }

                # Make JSON-RPC request
                response = await client.post(api_url, json=jsonrpc_request, headers=request_headers)

                # Check for cancellation after request
                if self.cancellation_checker and self.cancellation_checker():
                    logger.info("apflow API call cancelled after request")
                    return {
                        "success": False,
                        "error": "Call was cancelled",
                        "base_url": base_url,
                        "method": method,
                        "status_code": response.status_code,
                    }

                # Parse response
                if response.status_code != 200:
                    logger.error(
                        f"apflow API returned status {response.status_code}: {response.text}"
                    )
                    return {
                        "success": False,
                        "error": f"API returned status {response.status_code}",
                        "status_code": response.status_code,
                        "response": response.text,
                        "base_url": base_url,
                        "method": method,
                    }

                json_response = response.json()

                # Check for JSON-RPC error
                if "error" in json_response:
                    error = json_response["error"]
                    logger.error(f"apflow API error: {error}")
                    return {
                        "success": False,
                        "error": error.get("message", "Unknown error"),
                        "error_code": error.get("code"),
                        "error_data": error.get("data"),
                        "base_url": base_url,
                        "method": method,
                    }

                result_data = json_response.get("result", {})

                # Handle tasks.execute with wait_for_completion
                if method == "tasks.execute" and wait_for_completion:
                    task_id = result_data.get("task_id") or result_data.get("root_task_id")
                    if task_id:
                        logger.info(f"Waiting for task {task_id} to complete...")
                        final_result = await self._wait_for_task_completion(
                            base_url=base_url,
                            task_id=task_id,
                            auth_token=auth_token,
                            poll_interval=poll_interval,
                            timeout=timeout,
                            headers=headers,
                        )
                        return final_result

                return {
                    "success": True,
                    "result": result_data,
                    "base_url": base_url,
                    "method": method,
                }

        except httpx.TimeoutException:
            logger.error(f"apflow API call timeout after {timeout} seconds: {base_url}")
            return {
                "success": False,
                "error": f"Request timeout after {timeout} seconds",
                "base_url": base_url,
                "method": method,
            }
        except httpx.RequestError as e:
            logger.error(f"apflow API request error: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Request error: {str(e)}",
                "base_url": base_url,
                "method": method,
            }
        except Exception as e:
            logger.error(f"Unexpected error calling apflow API: {e}", exc_info=True)
            return {"success": False, "error": str(e), "base_url": base_url, "method": method}

    async def _wait_for_task_completion(
        self,
        base_url: str,
        task_id: str,
        auth_token: Optional[str],
        poll_interval: float,
        timeout: float,
        headers: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Wait for task completion by polling task status with production-grade retry logic

        Production features:
        - Exponential backoff on failures
        - Configurable failure thresholds
        - Error classification (retryable vs non-retryable)
        - Circuit breaker pattern for persistent failures
        - Detailed logging for debugging

        Args:
            base_url: API base URL
            task_id: Task ID to wait for
            auth_token: Optional auth token
            poll_interval: Base polling interval in seconds
            timeout: Total timeout in seconds
            headers: Additional headers

        Returns:
            Final task result
        """
        request_headers = {"Content-Type": "application/json", **headers}

        if auth_token:
            request_headers["Authorization"] = f"Bearer {auth_token}"

        api_url = f"{base_url.rstrip('/')}/tasks"
        # Validated once here, not per poll iteration — base_url is fixed for
        # the lifetime of this polling loop, so re-resolving DNS on every poll
        # would be wasted work.
        await validate_url_not_private(api_url, self.id)
        start_time = asyncio.get_event_loop().time()

        # Production-grade retry configuration
        max_consecutive_failures = 10  # Circuit breaker threshold
        max_total_failures = 20  # Total failure threshold across all polls
        base_backoff = poll_interval  # Base wait time
        max_backoff = 30.0  # Maximum backoff time (30 seconds)

        consecutive_failures = 0
        total_failures = 0
        poll_count = 0
        last_error = None

        while True:
            poll_count += 1

            # Check timeout
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                logger.warning(
                    f"Task {task_id} polling timeout after {elapsed:.2f}s "
                    f"(polls: {poll_count}, failures: {total_failures})"
                )
                return {
                    "success": False,
                    "error": f"Timeout waiting for task completion after {timeout} seconds",
                    "task_id": task_id,
                    "base_url": base_url,
                    "poll_count": poll_count,
                    "total_failures": total_failures,
                }

            # Check for cancellation
            if self.cancellation_checker and self.cancellation_checker():
                logger.info(f"Task {task_id} polling cancelled after {poll_count} polls")
                return {
                    "success": False,
                    "error": "Wait was cancelled",
                    "task_id": task_id,
                    "base_url": base_url,
                    "poll_count": poll_count,
                }

            # Poll task status
            poll_success = False
            current_backoff = base_backoff

            try:
                jsonrpc_request = {
                    "jsonrpc": "2.0",
                    "method": "tasks.get",
                    "params": {"task_id": task_id},
                    "id": f"apflow_poll_{asyncio.get_event_loop().time()}",
                }

                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        api_url, json=jsonrpc_request, headers=request_headers
                    )

                    if response.status_code == 200:
                        json_response = response.json()
                        if "result" in json_response:
                            task = json_response["result"]
                            status = task.get("status")

                            # Successful poll - reset failure counters
                            if consecutive_failures > 0:
                                logger.info(
                                    f"Task {task_id} polling recovered after {consecutive_failures} "
                                    f"consecutive failures"
                                )
                            consecutive_failures = 0
                            poll_success = True

                            if status in ("completed", "failed", "cancelled"):
                                logger.info(
                                    f"Task {task_id} finished with status: {status} "
                                    f"(polls: {poll_count}, failures: {total_failures})"
                                )
                                return {
                                    "success": status == "completed",
                                    "task_id": task_id,
                                    "status": status,
                                    "task": task,
                                    "base_url": base_url,
                                    "poll_count": poll_count,
                                    "total_failures": total_failures,
                                }

                            logger.debug(f"Task {task_id} status: {status}, waiting...")
                        else:
                            # Missing result field - treat as non-retryable error
                            consecutive_failures += 1
                            total_failures += 1
                            last_error = "Missing 'result' field in response"
                            logger.warning(
                                f"Task {task_id} poll {poll_count}: {last_error} "
                                f"(consecutive: {consecutive_failures}, total: {total_failures})"
                            )
                    elif 400 <= response.status_code < 500:
                        # Client error (4xx) - likely non-retryable
                        consecutive_failures += 1
                        total_failures += 1
                        last_error = f"Client error {response.status_code}: {response.text[:200]}"
                        logger.error(
                            f"Task {task_id} poll {poll_count}: {last_error} "
                            f"(consecutive: {consecutive_failures}, total: {total_failures})"
                        )
                        # For 4xx errors, we might want to fail faster
                        if consecutive_failures >= 3:
                            logger.error(
                                f"Task {task_id} polling stopped: too many client errors "
                                f"(likely invalid task_id or auth issue)"
                            )
                            return {
                                "success": False,
                                "error": f"Polling failed: {last_error}",
                                "task_id": task_id,
                                "base_url": base_url,
                                "poll_count": poll_count,
                                "total_failures": total_failures,
                                "error_type": "client_error",
                            }
                    else:
                        # Server error (5xx) - retryable
                        consecutive_failures += 1
                        total_failures += 1
                        last_error = f"Server error {response.status_code}: {response.text[:200]}"
                        logger.warning(
                            f"Task {task_id} poll {poll_count}: {last_error} "
                            f"(consecutive: {consecutive_failures}, total: {total_failures})"
                        )

            except httpx.TimeoutException as e:
                # Network timeout - retryable
                consecutive_failures += 1
                total_failures += 1
                last_error = f"Request timeout: {str(e)}"
                logger.warning(
                    f"Task {task_id} poll {poll_count}: {last_error} "
                    f"(consecutive: {consecutive_failures}, total: {total_failures})"
                )
            except httpx.RequestError as e:
                # Network error - retryable
                consecutive_failures += 1
                total_failures += 1
                last_error = f"Network error: {str(e)}"
                logger.warning(
                    f"Task {task_id} poll {poll_count}: {last_error} "
                    f"(consecutive: {consecutive_failures}, total: {total_failures})"
                )
            except Exception as e:
                # Unexpected error - log but continue
                consecutive_failures += 1
                total_failures += 1
                last_error = f"Unexpected error: {str(e)}"
                logger.error(
                    f"Task {task_id} poll {poll_count}: {last_error} "
                    f"(consecutive: {consecutive_failures}, total: {total_failures})",
                    exc_info=True,
                )

            # Circuit breaker: Stop if too many consecutive failures
            if consecutive_failures >= max_consecutive_failures:
                logger.error(
                    f"Task {task_id} polling circuit breaker triggered: "
                    f"{consecutive_failures} consecutive failures "
                    f"(total failures: {total_failures}, polls: {poll_count})"
                )
                return {
                    "success": False,
                    "error": f"Polling circuit breaker: {max_consecutive_failures} consecutive failures. Last error: {last_error}",
                    "task_id": task_id,
                    "base_url": base_url,
                    "poll_count": poll_count,
                    "total_failures": total_failures,
                    "consecutive_failures": consecutive_failures,
                    "error_type": "circuit_breaker",
                }

            # Total failure threshold
            if total_failures >= max_total_failures:
                logger.error(
                    f"Task {task_id} polling stopped: {total_failures} total failures "
                    f"(polls: {poll_count})"
                )
                return {
                    "success": False,
                    "error": f"Polling failed: {total_failures} total failures. Last error: {last_error}",
                    "task_id": task_id,
                    "base_url": base_url,
                    "poll_count": poll_count,
                    "total_failures": total_failures,
                    "error_type": "max_failures",
                }

            # Exponential backoff: Increase wait time on failures
            if not poll_success and consecutive_failures > 0:
                # Exponential backoff: base_backoff * 2^(failures-1), capped at max_backoff
                current_backoff = min(base_backoff * (2 ** (consecutive_failures - 1)), max_backoff)
                logger.debug(
                    f"Task {task_id} applying exponential backoff: {current_backoff:.2f}s "
                    f"(failures: {consecutive_failures})"
                )

            # Wait before next poll
            await asyncio.sleep(current_backoff)

    def get_demo_result(self, task: Any, inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Provide demo API call result"""
        method = inputs.get("method", "tasks.get")
        params = inputs.get("params", {})

        # Generate appropriate demo response based on method
        if method == "tasks.execute":
            return {
                "jsonrpc": "2.0",
                "result": {
                    "success": True,
                    "protocol": "jsonrpc",
                    "root_task_id": params.get("task_id", "demo-task-123"),
                    "status": "completed",
                },
                "id": "demo-request-id",
                "_demo_sleep": 0.4,  # Simulate API call and task execution time
            }
        elif method == "tasks.create":
            return {
                "jsonrpc": "2.0",
                "result": {
                    "success": True,
                    "tasks": [
                        {
                            "id": "demo-created-task-456",
                            "name": params.get("name", "demo_task"),
                            "status": "pending",
                        }
                    ],
                },
                "id": "demo-request-id",
                "_demo_sleep": 0.2,  # Simulate API call latency
            }
        elif method == "tasks.get":
            return {
                "jsonrpc": "2.0",
                "result": {
                    "id": params.get("task_id", "demo-task-123"),
                    "name": "demo_task",
                    "status": "completed",
                    "result": {"output": "Demo execution result"},
                },
                "id": "demo-request-id",
                "_demo_sleep": 0.2,  # Simulate API call latency
            }
        else:
            return {
                "jsonrpc": "2.0",
                "result": {"success": True, "message": f"Demo {method} completed"},
                "id": "demo-request-id",
                "_demo_sleep": 0.2,  # Simulate API call latency
            }
