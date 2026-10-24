"""Service connection policies keep credentials out of task definitions."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from apflow.app import ApflowApp
from starlette.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apflow.core.config_manager import get_config_manager
from apflow.core.execution.errors import ValidationError


@pytest.fixture
def connection_config(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = get_config_manager()
    monkeypatch.setattr(manager, "_data", dict(manager._data))
    manager.set(
        "network.profiles",
        [
            {
                "id": "world",
                "base_url": "http://127.0.0.1:4000/api/",
                "allow_private": True,
                "allowed_methods": ["POST"],
                "allowed_path_prefixes": ["/api/"],
                "credential_ref": "world-key",
            }
        ],
    )
    manager.set(
        "security.credentials",
        [
            {
                "id": "world-key",
                "env_var": "TEST_WORLDTOKEN",
                "allowed_profiles": ["world"],
            }
        ],
    )
    monkeypatch.setenv("TEST_WORLDTOKEN", "synthetic-credential")


def test_connection_resolution_keeps_original_inputs_secret_free(connection_config: None) -> None:
    from apflow.core.utils.credentials import resolve_outbound_inputs

    inputs: dict[str, Any] = {"connection_profile": "world", "method": "POST"}
    resolved = resolve_outbound_inputs(inputs)
    assert resolved["url"] == "http://127.0.0.1:4000/api/"
    assert resolved["headers"]["Authorization"] == "Bearer synthetic-credential"
    assert "headers" not in inputs


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://attacker.example/api/"},
        {"method": "GET"},
        {"url": "http://127.0.0.1:4000/admin"},
        {"url": "http://127.0.0.1:4000/api/../admin"},
        {"url": "http://127.0.0.1:4000/api/%2e%2e/admin"},
        {"headers": {"authorization": "override"}},
        {"credential_ref": "other"},
        {"verify": False},
    ],
)
def test_connection_rejects_target_and_credential_overrides(
    connection_config: None, changes: dict[str, Any]
) -> None:
    from apflow.core.utils.credentials import resolve_outbound_inputs

    with pytest.raises(ValidationError):
        resolve_outbound_inputs({"connection_profile": "world", "method": "POST", **changes})


@pytest.mark.asyncio
async def test_profile_allows_only_its_private_target(connection_config: None) -> None:
    from apflow.core.utils.network_security import validate_url_not_private

    await validate_url_not_private(
        "http://127.0.0.1:4000/api/run", "test", profile_id="world", method="POST"
    )
    with pytest.raises(ValidationError):
        await validate_url_not_private(
            "http://169.254.169.254/api/run", "test", profile_id="world", method="POST"
        )


def test_inline_credentials_rejected_and_public_data_redacted() -> None:
    from apflow.core.utils.credentials import redact_public_data, reject_inline_credentials

    data = {
        "inputs": {"headers": {"Authorization": "Bearer synthetic-token"}},
        "credential_ref": "world-key",
    }
    with pytest.raises(ValidationError, match="credential"):
        reject_inline_credentials(data)
    public = redact_public_data(data)
    assert "synthetic-token" not in str(public)
    assert public["credential_ref"] == "world-key"
    assert "synthetic-token" in str(data)


def test_profile_rejects_unknown_or_unavailable_credentials(
    connection_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apflow.core.utils.credentials import resolve_outbound_inputs

    monkeypatch.delenv("TEST_WORLDTOKEN")
    with pytest.raises(ValidationError, match="credential"):
        resolve_outbound_inputs({"connection_profile": "world", "method": "POST"})


def test_profile_rejects_wrong_subject(connection_config: None) -> None:
    from apflow.core.utils.credentials import resolve_outbound_inputs

    get_config_manager().get("network.profiles")[0]["allowed_subjects"] = ["alice"]
    with pytest.raises(ValidationError, match="subject"):
        resolve_outbound_inputs({"connection_profile": "world", "method": "POST"})


def test_connection_accepts_null_headers_and_redacts_echoed_secret(connection_config: None) -> None:
    from apflow.core.utils.credentials import redact_public_data, resolve_outbound_inputs

    resolved = resolve_outbound_inputs(
        {"connection_profile": "world", "method": "POST", "headers": None}
    )
    assert resolved["headers"]["Authorization"] == "Bearer synthetic-credential"
    assert redact_public_data({"echo": "synthetic-credential"}) == {"echo": "***REDACTED***"}


@pytest.mark.asyncio
async def test_profile_cannot_bypass_policy_with_legacy_private_switch(
    connection_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apflow.core.utils.network_security import validate_url_not_private

    monkeypatch.setenv("APFLOW_REST_ALLOW_PRIVATE_URLS", "1")
    with pytest.raises(ValidationError):
        await validate_url_not_private(
            "http://169.254.169.254/api/", "test", profile_id="world", method="POST"
        )


def test_outbound_schema_advertises_profile_as_alternative_target() -> None:
    from apflow.extensions.apflow.api_executor import ApFlowApiExecutor
    from apflow.extensions.http.rest_executor import RestExecutor

    assert RestExecutor().get_input_schema()["anyOf"] == [
        {"required": ["url"]},
        {"required": ["connection_profile"]},
    ]
    assert ApFlowApiExecutor().get_input_schema()["anyOf"] == [
        {"required": ["base_url"]},
        {"required": ["connection_profile"]},
    ]


def test_public_redaction_uses_sensitive_schema_references() -> None:
    from apflow.core.utils.credentials import redact_public_data

    schema = {
        "type": "object",
        "$defs": {"secret": {"type": "string", "x-sensitive": True}},
        "properties": {"custom": {"$ref": "#/$defs/secret"}},
    }
    output = redact_public_data({"custom": "private-value"}, schema=schema)
    assert output["custom"] != "private-value"


@pytest.mark.parametrize(
    "value",
    [
        {"headers": {"X-API-Key": "secret"}},
        {"auth": {"type": "apikey", "value": "secret"}},
        {"auth": {"type": "APIKEY", "value": "secret"}},
        {"_secret_provider_key": "secret"},
    ],
)
def test_inline_api_keys_are_rejected_and_redacted(value: dict[str, Any]) -> None:
    from apflow.core.utils.credentials import redact_public_data, reject_inline_credentials

    with pytest.raises(ValidationError):
        reject_inline_credentials(value)
    assert "'secret'" not in str(redact_public_data(value))


def test_trace_and_request_headers_use_execution_copy() -> None:
    from types import SimpleNamespace
    from apflow.core.utils.credentials import resolve_outbound_inputs

    inputs = {"url": "https://world.example/run", "headers": {"X-Request-ID": "operation-1"}}
    resolved = resolve_outbound_inputs(inputs, context=SimpleNamespace(trace_id="a" * 32))
    assert resolved["headers"]["traceparent"].startswith("00-" + "a" * 32 + "-")
    assert resolved["headers"]["X-Request-ID"] == "operation-1"
    assert "traceparent" not in inputs["headers"]


def test_executor_auth_schemas_mark_credentials_sensitive() -> None:
    from apflow.extensions.apflow.api_executor import ApFlowApiExecutor
    from apflow.extensions.http.rest_executor import RestExecutor

    schema = RestExecutor().get_input_schema()
    for field in ("token", "password", "value"):
        assert schema["properties"]["auth"]["anyOf"][0]["properties"][field]["x-sensitive"] is True
    assert schema["properties"]["headers"]["x-sensitive"] is True
    assert ApFlowApiExecutor().get_input_schema()["properties"]["auth_token"]["x-sensitive"] is True


@pytest.mark.asyncio
async def test_rest_executor_injects_credential_and_rejects_redirect(
    connection_config: None,
) -> None:
    import httpx
    from apflow.extensions.http.rest_executor import RestExecutor

    response = httpx.Response(
        302,
        headers={"Location": "http://127.0.0.1:4000/admin"},
        request=httpx.Request("POST", "http://127.0.0.1:4000/api/"),
    )
    client = AsyncMock()
    response.next_request = httpx.Request("GET", "http://127.0.0.1:4000/admin")
    client.request.return_value = response
    transport = MagicMock()
    transport.__aenter__.return_value = client
    with patch("httpx.AsyncClient", return_value=transport):
        with pytest.raises(ValidationError):
            await RestExecutor().execute({"connection_profile": "world", "method": "POST"})
    assert (
        client.request.call_args.kwargs["headers"]["Authorization"] == "Bearer synthetic-credential"
    )
    client.send.assert_not_called()


@pytest.mark.asyncio
async def test_api_executor_uses_connection_profile(connection_config: None) -> None:
    import httpx
    from apflow.extensions.apflow.api_executor import ApFlowApiExecutor

    profile = get_config_manager().get("network.profiles")[0]
    profile["base_url"] = "http://127.0.0.1:4000/"
    profile["allowed_path_prefixes"] = ["/modules/apflow.task.get"]
    client = AsyncMock()
    client.post.return_value = httpx.Response(200, json={"id": "task"})
    transport = MagicMock()
    transport.__aenter__.return_value = client
    with patch("httpx.AsyncClient", return_value=transport):
        output = await ApFlowApiExecutor().execute(
            {"connection_profile": "world", "method": "task.get", "params": {"task_id": "task"}}
        )
    assert output["result"]["success"] is True
    assert client.post.call_args.kwargs["headers"]["Authorization"] == "Bearer synthetic-credential"


@pytest.mark.parametrize("streaming", [False, True])
def test_rest_preserves_inbound_trace_and_verified_identity(streaming: bool) -> None:
    import json
    import jwt
    from apcore import Context, ModuleAnnotations, Registry
    from starlette.testclient import TestClient
    from apflow.api.auth import apply_rest_auth, build_mcp_authenticator
    from apflow.api.rest import build_rest_app
    from apflow.core.config_manager import ConfigManager

    class TraceProbe:
        description = "Inspect trace propagation"
        input_schema = {"type": "object"}
        output_schema = {"type": "object"}
        annotations = ModuleAnnotations()

        async def execute(self, inputs: dict[str, Any], context: Context[Any]) -> dict[str, Any]:
            return {
                "trace_id": context.trace_id,
                "owner": context.identity.id if context.identity else None,
            }

    registry = Registry()
    registry.register("trace.probe", TraceProbe())
    configuration = ConfigManager()
    secret = "synthetic-trace-test-key-with-32-bytes"
    configuration.set("api.jwt_secret", secret)
    authenticator = build_mcp_authenticator(configuration)
    trace_id = "a" * 32
    headers = {
        "Authorization": "Bearer " + jwt.encode({"sub": "alice"}, secret, algorithm="HS256"),
        "traceparent": f"00-{trace_id}-{'b' * 16}-01",
    }
    if streaming:
        headers["Accept"] = "text/event-stream"
    with TestClient(apply_rest_auth(build_rest_app(registry), authenticator)) as client:
        response = client.post("/modules/trace.probe", json={}, headers=headers)
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == trace_id
    if streaming:
        payload = json.loads(
            next(
                line[6:]
                for line in response.text.splitlines()
                if line.startswith("data: ") and '"owner"' in line
            )
        )
    else:
        payload = response.json()
    assert payload == {"trace_id": trace_id, "owner": "alice"}


CredentialService = tuple[ApflowApp, TestClient, dict[str, str]]


@pytest.fixture
def credential_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, connection_config: None
) -> Iterator[CredentialService]:
    import jwt
    from apflow.api.auth import apply_rest_auth, build_mcp_authenticator
    from apflow.api.rest import build_rest_app
    from apflow.app import create_app

    manager = get_config_manager()
    manager.set("security.require_identity", False)
    secret = "synthetic-credential-roundtrip-auth-key"
    manager.set("api.jwt_secret", secret)
    application = create_app(connection_string=f"sqlite:///{tmp_path / 'credential-service.db'}")
    monkeypatch.setattr("apflow.app.create_app", lambda: application)
    server = apply_rest_auth(build_rest_app(application.registry), build_mcp_authenticator(manager))
    headers = {"Authorization": "Bearer " + jwt.encode({"sub": "alice"}, secret, algorithm="HS256")}
    try:
        with TestClient(server) as client:
            yield application, client, headers
    finally:
        application.session.close()
        application.session.get_bind().engine.dispose()


def _execute_credential_profile(service: CredentialService) -> str:
    import httpx

    _, client, headers = service
    inputs = {"connection_profile": "world", "method": "POST"}
    created = client.post(
        "/modules/apflow.task.create",
        headers=headers,
        json={
            "name": "Credential roundtrip",
            "inputs": inputs,
            "params": {"executor_id": "rest_executor"},
        },
    )
    assert created.status_code == 200, created.text
    identifier = created.json()["id"]
    transport = AsyncMock()
    transport.request.return_value = httpx.Response(
        200,
        json={"echo": "synthetic-credential"},
        request=httpx.Request("POST", "http://127.0.0.1:4000/api/"),
    )
    with patch("httpx.AsyncClient") as constructor:
        constructor.return_value.__aenter__.return_value = transport
        executed = client.post(
            "/modules/apflow.task.execute", headers=headers, json={"task_id": identifier}
        )
    assert executed.status_code == 200 and executed.json()["status"] == "completed", executed.text
    assert (
        transport.request.call_args.kwargs["headers"]["Authorization"]
        == "Bearer synthetic-credential"
    )
    assert "synthetic-credential" not in executed.text
    return str(identifier)


def _assert_credential_public_reads(service: CredentialService, task_id: str) -> None:
    from click.testing import CliRunner
    from apflow.cli import _build_cli

    _, client, headers = service
    public = client.post("/modules/apflow.task.get", headers=headers, json={"task_id": task_id})
    assert public.status_code == 200
    assert public.json()["inputs"] == {"connection_profile": "world", "method": "POST"}
    assert "synthetic-credential" not in public.text
    cli = CliRunner().invoke(_build_cli(), ["task", "get", "--task-id", task_id])
    assert cli.exit_code == 0, cli.output
    assert task_id in cli.output and "synthetic-credential" not in cli.output


def test_service_credentials_stay_out_of_storage_rest_cli_copy_and_archive(
    credential_service: CredentialService,
) -> None:
    import json
    from sqlalchemy import select
    from apflow.core.storage.sqlalchemy.models import TaskModel, WorkflowRun

    application, client, headers = credential_service
    inputs = {"connection_profile": "world", "method": "POST"}
    identifier = _execute_credential_profile(credential_service)
    _assert_credential_public_reads(credential_service, identifier)
    copied = client.post("/modules/apflow.task.copy", headers=headers, json={"task_id": identifier})
    assert copied.status_code == 200, copied.text
    copy_id = copied.json()["root_task_id"]
    _assert_credential_public_reads(credential_service, copy_id)
    completed = client.post(
        "/modules/apflow.task.update",
        headers=headers,
        json={"task_id": copy_id, "status": "completed"},
    )
    assert completed.status_code == 200, completed.text
    archived = client.post(
        "/modules/apflow.task.archive", headers=headers, json={"task_id": copy_id}
    )
    assert archived.status_code == 200, archived.text
    _assert_credential_public_reads(credential_service, archived.json()["root_task_id"])
    with application.session_factory() as persisted:
        tasks = persisted.scalars(select(TaskModel)).all()
        assert len(tasks) == 2
        assert all(task.inputs == inputs for task in tasks)
        raw_rows = persisted.execute(select(TaskModel.__table__)).mappings().all()
        assert "synthetic-credential" not in json.dumps(
            [dict(row) for row in raw_rows], default=str
        )
        run = persisted.get(WorkflowRun, identifier)
        assert run is not None and "synthetic-credential" not in json.dumps(run.definition_snapshot)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("operation", ["create", "create_tree", "update", "copy"])
def test_authenticated_service_rejects_inline_credentials_before_persistence(
    credential_service: CredentialService,
    operation: str,
    streaming: bool,
) -> None:
    from sqlalchemy import select
    from apflow.core.storage.sqlalchemy.models import TaskModel

    application, client, headers = credential_service
    created = client.post(
        "/modules/apflow.task.create", headers=headers, json={"name": "Clean definition"}
    )
    assert created.status_code == 200
    identifier = created.json()["id"]
    sensitive = {"headers": {"Authorization": "Bearer synthetic-credential"}}
    requests = {
        "create": {"name": "Unsafe definition", "inputs": sensitive},
        "create_tree": {"tasks": [{"name": "Unsafe definition", "inputs": sensitive}]},
        "update": {"task_id": identifier, "inputs": sensitive},
        "copy": {"task_id": identifier, "overrides": {"inputs": sensitive}},
    }
    request_headers = {**headers, **({"Accept": "text/event-stream"} if streaming else {})}
    rejected = client.post(
        f"/modules/apflow.task.{operation}", headers=request_headers, json=requests[operation]
    )
    assert rejected.status_code == (200 if streaming else 422), rejected.text
    assert '"code": "INVALID_INPUT"' in rejected.text or '"code":"INVALID_INPUT"' in rejected.text
    if streaming:
        assert "event: error" in rejected.text and "event: done" in rejected.text
    assert "synthetic-credential" not in rejected.text
    with application.session_factory() as persisted:
        tasks = persisted.scalars(select(TaskModel)).all()
        assert len(tasks) == 1 and tasks[0].id == identifier and not tasks[0].inputs
