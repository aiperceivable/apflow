"""Fixed-path inbound webhook endpoint for external schedulers.

    POST /webhook/trigger/{task_id}

Lets cron, Kubernetes CronJob, Temporal, etc. push-trigger a scheduled task by
a stable URL. This is the operational sibling of the ``schedule.trigger``
registry module: same execution path (``WebhookGateway`` → mark running →
execute tree → advance next run), but addressed by path parameter and guarded
by its own HMAC signature / IP allowlist / rate limit rather than the registry
pipeline. Kept out of ``rest.py`` so the registry-driven REST adapter stays a
pure module face; it is injected via ``build_rest_app(extra_routes=...)``.

Auth note: the REST surface carries no JWT today, so this endpoint is disabled
by default and authenticates standalone via ``WebhookConfig`` (optional shared
secret). Enable it explicitly (``apflow rest --webhook`` / ``serve --webhook``).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from apflow.logger import get_logger

logger = get_logger(__name__)


def _coerce_bool(value: Any, default: bool) -> bool:
    """Parse a query/body flag (``"true"``/``"1"``/bool) into a bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def build_webhook_routes(
    *,
    secret_key: Optional[str] = None,
    allowed_ips: Optional[list[str]] = None,
    rate_limit: int = 0,
    async_execution: bool = True,
) -> list[Route]:
    """Build the inbound webhook route backed by a configured ``WebhookGateway``.

    Args:
        secret_key: Optional HMAC-SHA256 shared secret. When set, requests must
            carry ``X-Webhook-Signature`` (and optionally ``X-Webhook-Timestamp``).
        allowed_ips: Optional client IP allowlist (None = allow all).
        rate_limit: Max requests per minute per IP (0 = unlimited).
        async_execution: Default execution mode; per-request override via the
            ``async`` query parameter (``?async=false`` waits for the result).

    Returns:
        A single-element list with the ``POST /webhook/trigger/{task_id}`` route.
    """
    from apflow.scheduler.gateway.webhook import WebhookConfig, WebhookGateway

    config = WebhookConfig(
        secret_key=secret_key,
        allowed_ips=allowed_ips,
        rate_limit=rate_limit,
        async_execution=async_execution,
    )
    gateway = WebhookGateway(config)

    async def trigger(request: Request) -> Response:
        task_id = request.path_params["task_id"]
        body = await request.body()
        client_ip = request.client.host if request.client else ""

        validation = await gateway.validate_request(
            client_ip=client_ip,
            payload=body,
            signature=request.headers.get("X-Webhook-Signature"),
            timestamp=request.headers.get("X-Webhook-Timestamp"),
        )
        if not validation.get("valid"):
            return JSONResponse(
                {"error": {"code": "WEBHOOK_REJECTED", "message": validation.get("error", "")}},
                status_code=403,
            )

        # Optional owner check + per-request execution mode from the JSON body.
        user_id: Optional[str] = None
        execute_async = async_execution
        if body:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return JSONResponse(
                    {"error": {"code": "INVALID_JSON", "message": "Body must be JSON"}},
                    status_code=400,
                )
            if isinstance(payload, dict):
                user_id = payload.get("user_id")
                if "async_execution" in payload:
                    execute_async = _coerce_bool(payload.get("async_execution"), async_execution)
        if "async" in request.query_params:
            execute_async = _coerce_bool(request.query_params.get("async"), async_execution)

        result = await gateway.trigger_task(task_id, user_id=user_id, execute_async=execute_async)

        # A missing task is the only 404; an executed-but-failed task is a valid
        # 200 result (success=False) — external schedulers read the body, not
        # only the status — so the run is not treated as a transport error.
        error = str(result.get("error", "")) if not result.get("success") else ""
        if "not found" in error.lower():
            return JSONResponse(result, status_code=404)
        return JSONResponse(result, status_code=200)

    return [Route("/webhook/trigger/{task_id}", trigger, methods=["POST"])]
