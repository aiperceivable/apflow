"""Tests for the registry-driven REST adapter (apflow.api.rest)."""

from datetime import datetime, timezone
from typing import Any, Optional

import pytest
from apcore import ModuleAnnotations
from apcore.registry.registry import Registry
from starlette.testclient import TestClient

from apflow.api.rest import build_openapi, build_rest_app


class EchoModule:
    """Minimal duck-typed apcore module that echoes its input back."""

    description = "Echo the message back"
    input_schema = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }
    output_schema = {
        "type": "object",
        "properties": {"echo": {"type": "string"}},
    }

    def __init__(self) -> None:
        self.annotations = ModuleAnnotations()

    async def execute(
        self, inputs: dict[str, Any], context: Optional[Any] = None
    ) -> dict[str, Any]:
        return {"echo": inputs.get("message", "")}


class DatetimeModule:
    """A module that returns Python datetime values."""

    description = "Return a datetime"
    input_schema = {"type": "object"}
    output_schema = {"type": "object"}

    def __init__(self) -> None:
        self.annotations = ModuleAnnotations()

    async def execute(
        self, inputs: dict[str, Any], context: Optional[Any] = None
    ) -> dict[str, Any]:
        return {"created_at": datetime(2026, 7, 10, 12, 34, 56, tzinfo=timezone.utc)}


class StreamingEcho:
    """A streaming module that emits one chunk per step."""

    description = "Emit n incremental chunks"
    input_schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    output_schema = {"type": "object"}

    def __init__(self) -> None:
        self.annotations = ModuleAnnotations(streaming=True)

    async def execute(
        self, inputs: dict[str, Any], context: Optional[Any] = None
    ) -> dict[str, Any]:
        return {"chunks": inputs.get("n", 3)}

    async def stream(self, inputs: dict[str, Any], context: Any) -> Any:
        for i in range(inputs.get("n", 3)):
            yield {"chunk": i}


@pytest.fixture
def client() -> TestClient:
    registry = Registry()
    registry.register("echo", EchoModule())
    return TestClient(build_rest_app(registry, title="apflow-test", version="9.9.9"))


def test_healthz_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_root_reports_module_count(client: TestClient) -> None:
    body = client.get("/").json()
    assert body["name"] == "apflow-test"
    assert body["modules"] == 1
    assert body["docs"] == "/docs"


def test_list_modules_exposes_schema(client: TestClient) -> None:
    body = client.get("/modules").json()
    assert body["count"] == 1
    module = body["modules"][0]
    assert module["module_id"] == "echo"
    assert module["input_schema"]["properties"]["message"]["type"] == "string"


def test_openapi_has_module_path(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    assert spec["openapi"] == "3.1.0"
    op = spec["paths"]["/modules/echo"]["post"]
    assert op["operationId"] == "echo"
    assert op["requestBody"]["content"]["application/json"]["schema"]["required"] == ["message"]


def test_execute_module_returns_output(client: TestClient) -> None:
    resp = client.post("/modules/echo", json={"message": "hi"})
    assert resp.status_code == 200
    assert resp.json() == {"echo": "hi"}


def test_execute_module_serializes_datetime_output() -> None:
    registry = Registry()
    registry.register("datetime", DatetimeModule())
    client = TestClient(build_rest_app(registry))

    resp = client.post("/modules/datetime", json={})

    assert resp.status_code == 200
    assert resp.json() == {"created_at": "2026-07-10T12:34:56+00:00"}


def test_unknown_module_returns_404(client: TestClient) -> None:
    resp = client.post("/modules/nope", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MODULE_NOT_FOUND"


def test_invalid_json_body_returns_400(client: TestClient) -> None:
    resp = client.post(
        "/modules/echo",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_JSON"


def test_build_openapi_directly() -> None:
    registry = Registry()
    registry.register("echo", EchoModule())
    spec = build_openapi(registry, title="t", version="1.0", description="d")
    assert "/modules/echo" in spec["paths"]
    assert spec["info"] == {"title": "t", "version": "1.0", "description": "d"}


def test_execute_via_sse_falls_back_for_non_streaming_module(client: TestClient) -> None:
    resp = client.post(
        "/modules/echo",
        json={"message": "hi"},
        headers={"accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # Non-streaming modules emit a single data frame (the call_async result).
    assert '"echo"' in resp.text and '"hi"' in resp.text
    assert "event: done" in resp.text


def test_execute_via_sse_streams_each_chunk() -> None:
    registry = Registry()
    registry.register("stream_echo", StreamingEcho())
    client = TestClient(build_rest_app(registry))
    resp = client.post(
        "/modules/stream_echo",
        json={"n": 3},
        headers={"accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    assert resp.text.count("data:") >= 3
    assert '"chunk": 0' in resp.text and '"chunk": 2' in resp.text
    assert "event: done" in resp.text


def test_sse_unknown_module_returns_404(client: TestClient) -> None:
    resp = client.post(
        "/modules/nope",
        json={},
        headers={"accept": "text/event-stream"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MODULE_NOT_FOUND"
