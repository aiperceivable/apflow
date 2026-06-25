"""Tests for the unified REST+A2A+MCP mount assembly (apflow.api.combined)."""

from unittest.mock import patch

import pytest
from apcore.registry.registry import Registry
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from apflow.api.combined import _serve_all_async, assemble_combined
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


@pytest.mark.asyncio
async def test_serve_all_async_assembles_and_runs() -> None:
    """_serve_all_async builds REST, awaits the A2A app, enters the MCP context,
    assembles them, and runs uvicorn — verified with the apcore/uvicorn layers mocked."""
    a2a_stub = Starlette()
    mcp_stub = Starlette()
    served: dict = {}

    async def fake_a2a_serve(registry: object, **_kw: object) -> Starlette:
        return a2a_stub

    class _FakeMcpCtx:
        async def __aenter__(self) -> Starlette:
            return mcp_stub

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    def fake_mcp_serve(registry: object, **_kw: object) -> _FakeMcpCtx:
        return _FakeMcpCtx()

    class _FakeServer:
        def __init__(self, config: object) -> None:
            served["config"] = config

        async def serve(self) -> None:
            served["served"] = True

    with (
        patch("apcore_a2a.async_serve", fake_a2a_serve),
        patch("apcore_mcp.async_serve", fake_mcp_serve),
        patch("uvicorn.Server", _FakeServer),
    ):
        await _serve_all_async(
            Registry(),
            host="127.0.0.1",
            port=9999,
            title="apflow",
            version="1.0",
            description="d",
            cors_origins=None,
            log_level=None,
            explorer=False,
            metrics=False,
        )

    assert served.get("served") is True
    combined = served["config"].app  # the uvicorn.Config built around the combined app
    paths = [getattr(r, "path", None) for r in combined.routes]
    assert "/a2a" in paths  # A2A mounted under /a2a
    assert "/healthz" in paths  # REST routes lifted to root
