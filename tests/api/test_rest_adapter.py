"""Tests for the registry-driven REST adapter (apflow.api.rest)."""

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
