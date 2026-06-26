"""Tests for the inbound webhook endpoint (apflow.api.webhook).

POST /webhook/trigger/{task_id} drives WebhookGateway; these cover routing,
status mapping (200 run / 404 missing), and standalone HMAC authentication —
with the gateway's DB-backed execution mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from apflow.api.webhook import build_webhook_routes

_TRIGGER = "apflow.scheduler.gateway.webhook.WebhookGateway.trigger_task"


def _client(**kwargs: object) -> TestClient:
    return TestClient(Starlette(routes=build_webhook_routes(**kwargs)))


def test_webhook_triggers_task() -> None:
    result = {"success": True, "status": "triggered", "task_id": "t1"}
    with patch(_TRIGGER, new=AsyncMock(return_value=result)):
        resp = _client().post("/webhook/trigger/t1")
    assert resp.status_code == 200
    assert resp.json() == result


def test_webhook_missing_task_maps_to_404() -> None:
    result = {"success": False, "error": "Task t1 not found", "task_id": "t1"}
    with patch(_TRIGGER, new=AsyncMock(return_value=result)):
        resp = _client().post("/webhook/trigger/t1")
    assert resp.status_code == 404


def test_webhook_executed_failure_is_still_200() -> None:
    # A task that ran and failed is a valid result (success=False), not a transport
    # error — external schedulers read the body, so the status stays 200.
    result = {"success": False, "error": "boom during execution", "task_id": "t1"}
    with patch(_TRIGGER, new=AsyncMock(return_value=result)):
        resp = _client().post("/webhook/trigger/t1")
    assert resp.status_code == 200
    assert resp.json()["error"] == "boom during execution"


def test_webhook_rejects_missing_signature_when_secret_set() -> None:
    trigger = AsyncMock(return_value={"success": True})
    with patch(_TRIGGER, new=trigger):
        resp = _client(secret_key="s3cret").post("/webhook/trigger/t1")
    assert resp.status_code == 403
    trigger.assert_not_called()  # rejected before execution


def test_webhook_async_override_via_query() -> None:
    trigger = AsyncMock(return_value={"success": True, "status": "completed"})
    with patch(_TRIGGER, new=trigger):
        _client(async_execution=True).post("/webhook/trigger/t1?async=false")
    assert trigger.await_args is not None
    assert trigger.await_args.kwargs["execute_async"] is False
