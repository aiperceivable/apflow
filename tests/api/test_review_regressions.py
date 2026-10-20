"""Regression coverage for outbound executors and REST transport parity."""

import inspect
import json
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from apcore import ModuleAnnotations
from apcore.registry.registry import Registry
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from apflow.api.rest import build_rest_app
from apflow.bridge.task_modules import (
    TaskCreateModule,
    TaskDeleteModule,
    TaskGetModule,
    TaskListModule,
    TaskUpdateModule,
)
from apflow.core.execution.task_creator import TaskCreator
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.extensions.apflow.api_executor import ApFlowApiExecutor
from apflow.extensions.email.send_email_executor import SendEmailExecutor
from apflow.extensions.http.rest_executor import RestExecutor


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_refusal", [False, True])
async def test_smtp_obeys_send_signature_and_reports_recipient_refusal(
    partial_refusal: bool,
) -> None:
    aiosmtplib = pytest.importorskip("aiosmtplib")
    signature = inspect.signature(aiosmtplib.send)

    async def send(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], str]:
        signature.bind(*args, **kwargs)
        assert isinstance(args[0], EmailMessage)
        refused = {"rejected@example.com": (550, "Recipient rejected")} if partial_refusal else {}
        return refused, "Message accepted"

    inputs = {
        "provider": "smtp",
        "smtp_host": "smtp.example.com",
        "from_email": "sender@example.com",
        "to": ["accepted@example.com", "rejected@example.com"],
        "subject": "Delivery test",
        "body": "Test body",
    }
    with patch("apflow.extensions.email.send_email_executor.aiosmtplib.send", new=send):
        outcome = await SendEmailExecutor().execute(inputs)

    assert outcome["result"]["success"] is (not partial_refusal)
    if partial_refusal:
        assert "rejected@example.com" in outcome["result"]["refused_recipients"]
        assert outcome["result"]["accepted_recipients"] == ["accepted@example.com"]
        assert outcome["error"]
    else:
        assert "error" not in outcome


@pytest.mark.asyncio
async def test_rest_executor_accepts_null_headers() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client_type = httpx.AsyncClient
    with (
        patch("apflow.extensions.http.rest_executor.validate_url_not_private", new=AsyncMock()),
        patch(
            "httpx.AsyncClient",
            side_effect=lambda **kwargs: client_type(
                transport=httpx.MockTransport(respond), **kwargs
            ),
        ),
    ):
        result = await RestExecutor(headers={"X-Default": "kept"}).execute(
            {"url": "https://example.com/resource", "headers": None}
        )
    assert result["result"]["success"] is True
    assert seen[0].headers["X-Default"] == "kept"


@pytest.mark.asyncio
async def test_remote_executor_calls_current_registry_rest_modules(
    sync_db_session: Session,
) -> None:
    repository = TaskRepository(sync_db_session)
    registry = Registry()
    registry.register(
        "apflow.task.create", TaskCreateModule(TaskCreator(sync_db_session), repository)
    )
    registry.register("apflow.task.get", TaskGetModule(repository))
    registry.register("apflow.task.update", TaskUpdateModule(repository))
    registry.register("apflow.task.list", TaskListModule(repository))
    registry.register("apflow.task.delete", TaskDeleteModule(repository))
    app = build_rest_app(registry)
    client_type = httpx.AsyncClient
    executor = ApFlowApiExecutor()

    async def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await executor.execute(
            {"base_url": "http://remote.test", "method": method, "params": params, "headers": None}
        )
        assert response["result"]["success"] is True, response
        return response["result"]["result"]

    with (
        patch("apflow.extensions.apflow.api_executor.validate_url_not_private", new=AsyncMock()),
        patch(
            "httpx.AsyncClient",
            side_effect=lambda **kwargs: client_type(
                transport=httpx.ASGITransport(app=app), **kwargs
            ),
        ),
    ):
        created = await call("tasks.create", {"name": "Remote task"})
        task_id = created["id"]
        updated = await call("tasks.update", {"task_id": task_id, "result": {"nested": 42}})
        assert updated["result"] == {"nested": 42}
        fetched = await call("apflow.task.get", {"task_id": task_id})
        assert fetched["id"] == task_id
        assert fetched["result"] == {"nested": 42}
        listed = await call("tasks.list", {})
        assert [task["id"] for task in listed["tasks"]] == [task_id]
        deleted = await call("tasks.delete", {"task_id": task_id})
        assert deleted["deleted"] is True
        missing = await executor.execute(
            {
                "base_url": "http://remote.test",
                "method": "tasks.get",
                "params": {"task_id": task_id},
            }
        )
        assert missing["result"]["success"] is False
        assert missing["result"]["error_code"] == "MODULE_NOT_FOUND"


@pytest.mark.asyncio
async def test_remote_polling_reads_current_rest_task_output_with_null_headers() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "task-123", "status": "completed"})

    client_type = httpx.AsyncClient
    with (
        patch("apflow.extensions.apflow.api_executor.validate_url_not_private", new=AsyncMock()),
        patch(
            "httpx.AsyncClient",
            side_effect=lambda **kwargs: client_type(
                transport=httpx.MockTransport(respond), **kwargs
            ),
        ),
    ):
        result = await ApFlowApiExecutor()._wait_for_task_completion(
            "http://remote.test", "task-123", None, 0, 1, None
        )
    assert result["status"] == "completed"
    assert len(requests) == 1
    assert requests[0].url.path == "/modules/apflow.task.get"
    assert json.loads(requests[0].content) == {"task_id": "task-123"}


@pytest.mark.parametrize("streaming", [False, True])
def test_sse_serializes_datetime_like_json(streaming: bool) -> None:
    class DatetimeModule:
        description = "Return a datetime"
        annotations = ModuleAnnotations(streaming=streaming)
        input_schema = {"type": "object"}
        output_schema = {"type": "object"}

        async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
            return {"created_at": datetime(2026, 7, 10, tzinfo=timezone.utc)}

        async def stream(self, inputs: dict[str, Any], context: Any) -> Any:
            yield await self.execute(inputs, context)

    registry = Registry()
    registry.register("datetime", DatetimeModule())
    with TestClient(build_rest_app(registry)) as client:
        plain = client.post("/modules/datetime", json={}).json()
        stream = client.post(
            "/modules/datetime", json={}, headers={"Accept": "text/event-stream"}
        ).text
    assert f"data: {json.dumps(plain)}\n\n" in stream
    assert "event: error" not in stream
    assert "event: done" in stream
