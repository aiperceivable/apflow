"""Regression tests for security / error-handling review fixes."""

from __future__ import annotations

import socket
import time

import pytest

from apflow.core.execution.errors import ValidationError
from apflow.core.storage.factory import _redact_connection_string
from apflow.core.utils.network_security import validate_url_not_private


def test_redact_connection_string_hides_password() -> None:
    redacted = _redact_connection_string("postgresql://user:secret@host:5432/db")
    assert "secret" not in redacted
    assert "user" in redacted and "host" in redacted


def test_redact_connection_string_handles_empty() -> None:
    assert _redact_connection_string(None) == ""
    assert _redact_connection_string("") == ""


@pytest.mark.asyncio
async def test_rest_execute_does_not_leak_exception_text() -> None:
    from apflow.api.rest import _execute

    class _Boom:
        async def call_async(self, module_id, inputs):  # noqa: ANN001
            raise RuntimeError("secret DSN postgresql://u:p@h/db in message")

    status, payload = await _execute(_Boom(), "apflow.task.get", {})
    assert status == 500
    # The raw exception text (which can carry DSNs / hostnames) must not reach the client.
    assert payload["error"]["message"] == "Internal server error"
    assert "postgresql://" not in str(payload)


@pytest.mark.asyncio
async def test_validate_url_times_out_on_slow_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow resolver must not overrun the request budget: resolution is bounded."""

    def _slow_getaddrinfo(*_args, **_kwargs):
        time.sleep(1.0)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", _slow_getaddrinfo)
    with pytest.raises(ValidationError, match="Timed out resolving"):
        await validate_url_not_private("http://slow.example.com/", "rest", resolve_timeout=0.05)
