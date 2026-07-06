"""
Webhook Gateway for External Schedulers

Provides HTTP webhook endpoints for external schedulers to trigger task execution.
This enables integration with cron, Kubernetes CronJob, APScheduler, Temporal, etc.

Usage patterns:

1. Direct trigger (Push mode):
   External scheduler calls: POST /webhook/trigger/{task_id}

2. Poll and execute (Pull mode):
   External scheduler polls: GET /api/tasks.scheduled.due
   Then executes each task via: POST /api/tasks.execute

The webhook gateway supports both patterns, defaulting to the simpler direct trigger.
"""

import asyncio
import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from apflow.logger import get_logger

logger = get_logger(__name__)


@dataclass
class WebhookConfig:
    """
    Configuration for webhook gateway.

    Attributes:
        secret_key: Optional secret for HMAC signature verification
        allowed_ips: Optional list of allowed IP addresses (None = allow all)
        rate_limit: Maximum requests per minute per IP (0 = unlimited)
        timeout: Timeout for task execution in seconds
        async_execution: If True, return immediately and execute in background
    """

    secret_key: Optional[str] = None
    allowed_ips: Optional[List[str]] = None
    rate_limit: int = 0
    timeout: int = 3600
    async_execution: bool = True
    # Rate limiting state
    _rate_limit_state: Dict[str, List[float]] = field(default_factory=dict)


class WebhookGateway:
    """
    Webhook gateway for external scheduler integration.

    Provides methods for:
    - Validating webhook requests (signature, IP, rate limit)
    - Triggering task execution
    - Generating webhook URLs for external schedulers

    Example usage with FastAPI:

        from fastapi import FastAPI, Request, HTTPException
        from apflow.scheduler.gateway import WebhookGateway, WebhookConfig

        app = FastAPI()
        config = WebhookConfig(secret_key="your-secret")
        gateway = WebhookGateway(config)

        @app.post("/webhook/trigger/{task_id}")
        async def trigger_task(task_id: str, request: Request):
            # Validate request
            if not await gateway.validate_request(request):
                raise HTTPException(status_code=403, detail="Invalid request")

            # Trigger task
            result = await gateway.trigger_task(task_id)
            return result
    """

    def __init__(self, config: Optional[WebhookConfig] = None):
        """
        Initialize webhook gateway.

        Args:
            config: Webhook configuration. Uses defaults if not provided.
        """
        self.config = config or WebhookConfig()
        self._execution_callbacks: List[Callable] = []

    def validate_signature(
        self,
        payload: bytes,
        signature: str,
        timestamp: Optional[str] = None,
    ) -> bool:
        """
        Validate HMAC signature of webhook request.

        The signature should be computed as:
        HMAC-SHA256(secret_key, timestamp + "." + payload)

        Args:
            payload: Request body bytes
            signature: Provided signature (hex encoded)
            timestamp: Request timestamp (for replay protection)

        Returns:
            True if signature is valid
        """
        if not self.config.secret_key:
            return True  # No secret configured, skip validation

        try:
            # Build message to sign
            if timestamp:
                message = f"{timestamp}.".encode() + payload
            else:
                message = payload

            # Compute expected signature
            expected = hmac.new(
                self.config.secret_key.encode(), message, hashlib.sha256
            ).hexdigest()

            # Constant-time comparison
            return hmac.compare_digest(expected, signature)

        except Exception as e:
            logger.warning(f"Signature validation error: {e}")
            return False

    def validate_ip(self, client_ip: str) -> bool:
        """
        Validate client IP address.

        Args:
            client_ip: Client IP address

        Returns:
            True if IP is allowed
        """
        if not self.config.allowed_ips:
            return True  # No IP restriction

        return client_ip in self.config.allowed_ips

    def check_rate_limit(self, client_ip: str) -> bool:
        """
        Check if request is within rate limit.

        Args:
            client_ip: Client IP address

        Returns:
            True if within rate limit
        """
        if self.config.rate_limit <= 0:
            return True  # No rate limit

        now = time.time()
        window_start = now - 60  # 1 minute window

        # Get request timestamps for this IP
        timestamps = self.config._rate_limit_state.get(client_ip, [])

        # Remove old timestamps
        timestamps = [ts for ts in timestamps if ts > window_start]

        # Check limit
        if len(timestamps) >= self.config.rate_limit:
            return False

        # Record this request
        timestamps.append(now)
        self.config._rate_limit_state[client_ip] = timestamps

        return True

    async def validate_request(
        self,
        client_ip: str,
        payload: Optional[bytes] = None,
        signature: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Validate a webhook request.

        Args:
            client_ip: Client IP address
            payload: Request body (for signature validation)
            signature: Request signature header
            timestamp: Request timestamp header

        Returns:
            Dict with 'valid' bool and 'error' message if invalid
        """
        # Check IP
        if not self.validate_ip(client_ip):
            return {"valid": False, "error": "IP not allowed"}

        # Check rate limit
        if not self.check_rate_limit(client_ip):
            return {"valid": False, "error": "Rate limit exceeded"}

        # Check signature if configured. A configured secret requires EVERY request
        # to be signed — including empty-body triggers, where task_id lives in the
        # URL path — so the HMAC guard cannot be bypassed by omitting the body.
        if self.config.secret_key:
            if not signature:
                return {"valid": False, "error": "Missing signature"}
            if not self.validate_signature(payload or b"", signature, timestamp):
                return {"valid": False, "error": "Invalid signature"}

        return {"valid": True}

    async def trigger_task(
        self,
        task_id: str,
        user_id: Optional[str] = None,
        execute_async: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Trigger execution of a scheduled task.

        Args:
            task_id: Task ID to execute
            user_id: Optional user ID for permission check
            execute_async: Override config.async_execution for this call

        Returns:
            Dict with execution result or status
        """
        async_exec = execute_async if execute_async is not None else self.config.async_execution

        logger.info(f"Webhook trigger for task {task_id} (async={async_exec})")

        try:
            from apflow.core.storage import create_pooled_session
            from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

            # Verify task exists and check permissions
            async with create_pooled_session() as db_session:
                task_repository = TaskRepository(db_session)
                task = await task_repository.get_task_by_id(task_id)

                if not task:
                    return {
                        "success": False,
                        "error": f"Task {task_id} not found",
                        "task_id": task_id,
                    }

                # Check user_id if provided
                if user_id and task.user_id and task.user_id != user_id:
                    return {
                        "success": False,
                        "error": "Permission denied",
                        "task_id": task_id,
                    }

            # Clone-per-fire: _execute_task spawns a fresh run instance from the
            # definition and executes that. The definition is never executed in
            # place, so it is not marked in_progress here.
            if async_exec:
                # Execute in background
                asyncio.create_task(self._execute_task_background(task_id))
                return {
                    "success": True,
                    "status": "triggered",
                    "task_id": task_id,
                    "message": "Task execution started in background",
                }
            else:
                # Execute synchronously
                result = await self._execute_task(task_id)
                return result

        except Exception as e:
            logger.error(f"Failed to trigger task {task_id}: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "task_id": task_id,
            }

    async def _execute_task(self, task_id: str) -> Dict[str, Any]:
        """
        Execute a task synchronously.

        Args:
            task_id: Task ID to execute

        Returns:
            Execution result
        """
        from apflow.core.storage import create_pooled_session
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
        from apflow.core.execution.task_creator import TaskCreator
        from apflow.core.execution.task_executor import TaskExecutor

        try:
            async with create_pooled_session() as db_session:
                task_repository = TaskRepository(db_session)
                task_executor = TaskExecutor()

                # Get the scheduled definition.
                definition = await task_repository.get_task_by_id(task_id)
                if not definition:
                    return {
                        "success": False,
                        "error": "Task not found",
                        "task_id": task_id,
                    }

                # Spawn a fresh run instance from the definition. The definition is
                # never executed in place — the clone is what runs, giving isolated
                # per-fire history (origin_type=scheduled_run) and matching the
                # distributed one-task-id-per-run model.
                run_tree = await TaskCreator(db_session).instantiate_scheduled_run(definition)
                run_id = str(run_tree.task.id)

                # Load the run tree from DB — unified with the tree execution model.
                run = await task_repository.get_task_by_id(run_id)
                if run is None:
                    return {
                        "success": False,
                        "error": "Run instance disappeared after creation",
                        "task_id": task_id,
                    }
                run_tree = await task_repository.get_task_tree_for_api(run)
                logger.info(
                    f"Executing run {run_id} for schedule {task_id}: "
                    f"{len(run_tree.children)} children"
                )

                await task_executor.execute_task_tree(
                    task_tree=run_tree,
                    root_task_id=run_id,
                    use_streaming=False,
                    db_session=db_session,
                )

                # Capture the run instance's terminal state.
                run = await task_repository.get_task_by_id(run_id)
                if run is None:
                    return {
                        "success": False,
                        "error": "Run instance disappeared during execution",
                        "task_id": task_id,
                    }
                task_status = run.status
                task_result = run.result
                task_error = run.error
                execution_success = task_status == "completed"

                # Collect children results for visibility
                children_results = []
                if getattr(run, "has_children", False):
                    children = await task_repository.get_child_tasks_by_parent_id(run_id)
                    for child in children:
                        children_results.append(
                            {
                                "task_id": child.id,
                                "name": getattr(child, "name", ""),
                                "status": child.status,
                                "error": child.error,
                            }
                        )

                # Advance the definition's schedule for the next run.
                await task_repository.complete_scheduled_run(
                    task_id=task_id,
                    success=execution_success,
                    error=task_error,
                    calculate_next_run=True,
                )

                return {
                    "success": execution_success,
                    "status": task_status,
                    "task_id": task_id,
                    "run_id": run_id,
                    "result": task_result,
                    "error": task_error,
                    "children": children_results,
                }

        except Exception as e:
            logger.error(f"Task execution error: {e}", exc_info=True)

            # Try to complete schedule tracking even on failure
            try:
                async with create_pooled_session() as db_session:
                    task_repository = TaskRepository(db_session)
                    await task_repository.complete_scheduled_run(
                        task_id=task_id,
                        success=False,
                        error=str(e),
                    )
            except Exception:
                pass

            return {
                "success": False,
                "error": str(e),
                "task_id": task_id,
            }

    async def _execute_task_background(self, task_id: str) -> None:
        """
        Execute a task in the background.

        Args:
            task_id: Task ID to execute
        """
        result = await self._execute_task(task_id)

        # Notify callbacks
        for callback in self._execution_callbacks:
            try:
                callback(task_id, result)
            except Exception as e:
                logger.warning(f"Callback error: {e}")

    def on_execution_complete(self, callback: Callable[[str, Dict], None]) -> None:
        """
        Register a callback for execution completion.

        Args:
            callback: Function called with (task_id, result)
        """
        self._execution_callbacks.append(callback)

    def generate_webhook_url(
        self,
        task_id: str,
        base_url: str,
        include_signature: bool = False,
    ) -> Dict[str, str]:
        """
        Generate webhook URL for external scheduler configuration.

        Args:
            task_id: Task ID
            base_url: Base URL of the apflow API
            include_signature: Include signature instructions

        Returns:
            Dict with URL and optional signature instructions
        """
        url = f"{base_url.rstrip('/')}/webhook/trigger/{task_id}"

        result = {"url": url, "method": "POST"}

        if include_signature and self.config.secret_key:
            result["signature_header"] = "X-Webhook-Signature"
            result["timestamp_header"] = "X-Webhook-Timestamp"
            result["signature_algorithm"] = "HMAC-SHA256"
            result["signature_format"] = "hex(HMAC-SHA256(secret, timestamp + '.' + body))"

        return result


def generate_cron_config(
    task_id: str,
    schedule_expression: str,
    webhook_url: str,
) -> str:
    """
    Generate crontab entry for a scheduled task.

    Args:
        task_id: Task ID
        schedule_expression: Cron expression (e.g., "0 9 * * 1-5")
        webhook_url: Webhook URL to call

    Returns:
        Crontab entry string
    """
    # Parse cron expression to standard crontab format
    # Assumes schedule_expression is already in cron format
    return f"{schedule_expression} curl -X POST {webhook_url} # apflow task {task_id}"


def generate_kubernetes_cronjob(
    task_id: str,
    task_name: str,
    schedule_expression: str,
    webhook_url: str,
    namespace: str = "default",
) -> Dict[str, Any]:
    """
    Generate Kubernetes CronJob manifest for a scheduled task.

    Args:
        task_id: Task ID
        task_name: Task name (for metadata)
        schedule_expression: Cron expression
        webhook_url: Webhook URL to call
        namespace: Kubernetes namespace

    Returns:
        Kubernetes CronJob manifest as dict
    """
    # Sanitize name for Kubernetes (lowercase, alphanumeric, hyphens only)
    k8s_name = f"apflow-{task_id[:8]}".lower().replace("_", "-")

    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {
            "name": k8s_name,
            "namespace": namespace,
            "labels": {
                "app": "apflow",
                "task-id": task_id,
            },
            "annotations": {
                "apflow.io/task-name": task_name,
            },
        },
        "spec": {
            "schedule": schedule_expression,
            "jobTemplate": {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "trigger",
                                    "image": "curlimages/curl:latest",
                                    "args": [
                                        "-X",
                                        "POST",
                                        "-H",
                                        "Content-Type: application/json",
                                        webhook_url,
                                    ],
                                }
                            ],
                            "restartPolicy": "OnFailure",
                        }
                    }
                }
            },
        },
    }
