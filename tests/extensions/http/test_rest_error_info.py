"""HTTP failure classification must preserve safe replay boundaries."""

from unittest.mock import patch

import httpx
import pytest

from apflow.core.execution.errors import NetworkError
from apflow.extensions.http.rest_executor import RestExecutor


@pytest.mark.asyncio
@pytest.mark.parametrize("method,retryable", [("GET", True), ("POST", False)])
async def test_http_failure_classifies_retry_and_upstream_code(
    monkeypatch: pytest.MonkeyPatch, method: str, retryable: bool
) -> None:
    monkeypatch.setenv("APFLOW_REST_ALLOW_PRIVATE_URLS", "1")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            503,
            json={"error": {"code": "WORLD_BUSY"}},
            headers={"Retry-After": "2", "X-Request-ID": "request-1"},
            request=request,
        )
    )
    client = httpx.AsyncClient(transport=transport)
    with patch("httpx.AsyncClient", return_value=client):
        result = await RestExecutor().execute(
            {"url": "https://world.example/run", "method": method}
        )
    info = result["error_info"]
    assert info["retryable"] is retryable
    assert info["upstream_code"] == "WORLD_BUSY"
    assert info["request_id"] == "request-1"
    assert info["http_status"] == 503
    assert info.get("retry_after_seconds") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("method,retryable", [("GET", True), ("POST", False)])
async def test_read_timeout_keeps_unknown_delivery(
    monkeypatch: pytest.MonkeyPatch, method: str, retryable: bool
) -> None:
    monkeypatch.setenv("APFLOW_REST_ALLOW_PRIVATE_URLS", "1")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Response lost", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    with patch("httpx.AsyncClient", return_value=client), pytest.raises(NetworkError) as failure:
        await RestExecutor().execute({"url": "https://world.example/run", "method": method})
    assert failure.value.error_info is not None
    assert failure.value.error_info["delivery_state"] == "unknown"
    assert failure.value.error_info["retryable"] is retryable


@pytest.mark.asyncio
async def test_api_failure_preserves_http_error_info(monkeypatch: pytest.MonkeyPatch) -> None:
    from apflow.extensions.apflow.api_executor import ApFlowApiExecutor

    monkeypatch.setenv("APFLOW_REST_ALLOW_PRIVATE_URLS", "1")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                503,
                json={"error": {"code": "REMOTE_BUSY", "message": "Busy"}},
                request=request,
                headers={"X-Request-ID": "remote-1"},
            )
        )
    )
    with patch("httpx.AsyncClient", return_value=client):
        output = await ApFlowApiExecutor().execute(
            {
                "base_url": "https://world.example",
                "method": "task.get",
                "params": {"task_id": "task"},
            }
        )
    assert output["error_info"]["upstream_code"] == "REMOTE_BUSY"
    assert output["error_info"]["request_id"] == "remote-1"
    assert output["error_info"]["retryable"] is False


@pytest.mark.asyncio
async def test_redirect_connection_failure_does_not_replay_original_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APFLOW_REST_ALLOW_PRIVATE_URLS", "1")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/submit":
            return httpx.Response(303, headers={"Location": "/status"}, request=request)
        raise httpx.ConnectError("Connection lost after submission", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with patch("httpx.AsyncClient", return_value=client), pytest.raises(NetworkError) as failure:
        await RestExecutor().execute({"url": "https://world.example/submit", "method": "POST"})
    assert failure.value.error_info is not None
    assert failure.value.error_info["delivery_state"] == "unknown"
    assert failure.value.error_info["retryable"] is False
