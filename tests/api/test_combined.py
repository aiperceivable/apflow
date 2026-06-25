"""Tests for the unified REST+A2A+MCP mount assembly (apflow.api.combined)."""

from apcore.registry.registry import Registry
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from apflow.api.combined import assemble_combined
from apflow.api.rest import build_rest_app


def _stub(path: str, label: str) -> Starlette:
    """A throwaway sub-app exposing a single route returning its label."""

    async def handler(_request: Request) -> Response:
        return PlainTextResponse(label)

    return Starlette(routes=[Route(path, handler)])


def _combined() -> TestClient:
    rest_app = build_rest_app(Registry(), title="apflow", version="1.0")
    a2a = _stub("/ping", "a2a")
    mcp = _stub("/mcp", "mcp")
    return TestClient(assemble_combined(rest_app, a2a, mcp))


def test_rest_owns_root_paths() -> None:
    client = _combined()
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/").json()["name"] == "apflow"


def test_a2a_mounted_under_prefix() -> None:
    assert _combined().get("/a2a/ping").text == "a2a"


def test_mcp_endpoint_stays_at_clean_top_level_path() -> None:
    # MCP is a root catch-all so its /mcp endpoint is NOT buried at /mcp/mcp.
    assert _combined().get("/mcp").text == "mcp"


def test_rest_404_envelope_preserved() -> None:
    resp = _combined().post("/modules/nope", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MODULE_NOT_FOUND"
