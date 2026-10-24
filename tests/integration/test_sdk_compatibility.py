"""Exercise apflow modules through the installed protocol SDKs."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from types import SimpleNamespace

import httpx
import pytest
from apcore import Executor, Registry
from click.testing import CliRunner
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent
from sqlalchemy.orm import Session
from sqlalchemy.engine import Engine

from apflow.api.combined import _serve_all_async
from apflow.app import ApflowApp, create_app
from apflow.bridge.module_adapter import ExecutableTaskModuleAdapter
from apflow.bridge.protocol import ApflowMCP, PublicExecutor
from apflow.cli import _build_cli, mcp, serve


@pytest.fixture
def sdk_app(tmp_path: Path) -> Iterator[ApflowApp]:
    app = create_app(connection_string=f"sqlite:///{tmp_path / 'sdk.db'}")
    assert isinstance(app.session, Session)
    engine = app.session.get_bind()
    assert isinstance(engine, Engine)
    try:
        yield app
    finally:
        app.session.close()
        engine.dispose()


@asynccontextmanager
async def _mcp_client(
    app: ApflowApp, executor: Executor | None = None, *, trace: bool = False
) -> AsyncIterator[ClientSession]:
    runtime = ApflowMCP(
        executor or app.registry, name="apflow-test", require_auth=False, trace=trace
    )
    server, _, _, _, _ = runtime._build_server_components()
    async with create_connected_server_and_client_session(server) as client:
        yield client


ModuleCall = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@pytest.fixture
def strict_output_app(tmp_path: Path, reset_storage_singleton: None) -> Iterator[ApflowApp]:
    from apflow.core.base import BaseTask
    from apflow.core.extensions import get_registry

    class StrictOutputExecutor(BaseTask):
        id = "strict_output_probe"
        name = "Strict output probe"
        description = "Return valid sensitive values with constrained output types."
        inputs_schema = {"type": "object", "properties": {"invalid": {"type": "boolean"}}}
        outputs_schema = {
            "type": "object",
            "properties": {
                "token": {
                    "type": "object",
                    "properties": {"expires_in": {"type": "integer"}},
                    "required": ["expires_in"],
                },
                "private": {"type": "integer", "x-sensitive": True},
                "receipt": {"enum": ["private-receipt"], "x-sensitive": True},
                "token_usage": {"type": "object"},
            },
            "required": ["token", "private", "receipt", "token_usage"],
        }

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            return {
                "token": {"expires_in": "invalid" if inputs.get("invalid") else 30},
                "private": 123,
                "receipt": "private-receipt",
                "token_usage": {"total": 7},
            }

    registry = get_registry()
    registry.register(StrictOutputExecutor(), executor_class=StrictOutputExecutor)
    application = create_app(connection_string=f"sqlite:///{tmp_path / 'strict-output.db'}")
    try:
        yield application
    finally:
        application.session.close()
        application.session.get_bind().engine.dispose()
        registry.unregister(StrictOutputExecutor.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol",
    ["native", "native-stream", "public", "public-trace", "public-stream", "rest", "rest-stream"],
)
async def test_direct_executor_validates_before_public_redaction(
    strict_output_app: ApflowApp, protocol: str
) -> None:
    from apflow.api.rest import build_rest_app
    from starlette.testclient import TestClient

    module_id = "apflow.strict_output_probe"
    native = Executor.from_registry(strict_output_app.registry)
    public = PublicExecutor(native)
    if protocol.startswith("rest"):
        with TestClient(build_rest_app(strict_output_app.registry)) as client:
            response = client.post(
                f"/modules/{module_id}",
                json={},
                headers={"Accept": "text/event-stream"} if protocol == "rest-stream" else {},
            )
        assert response.status_code == 200, response.text
        assert "event: error" not in response.text
        output = (
            json.loads(response.text.split("data: ", 1)[1].splitlines()[0])
            if protocol == "rest-stream"
            else response.json()
        )
    elif protocol in {"native-stream", "public-stream"}:
        executor = native if protocol == "native-stream" else public
        output = [chunk async for chunk in executor.stream(module_id, {})][0]
    elif protocol == "public-trace":
        output, _trace = await public.call_async_with_trace(module_id, {})
    else:
        output = await (native if protocol == "native" else public).call_async(module_id, {})
    assert output["token_usage"] == {"total": 7}
    if protocol.startswith("native"):
        assert output["token"] == {"expires_in": 30}
        assert output["private"] == 123
        assert output["receipt"] == "private-receipt"
    else:
        assert output["token"] == output["private"] == output["receipt"] == "***REDACTED***"


@pytest.mark.asyncio
@pytest.mark.parametrize("inputs", [{"invalid": True}, {"invalid": "wrong-type"}])
async def test_direct_executor_rejects_invalid_input_and_output_before_redaction(
    strict_output_app: ApflowApp, inputs: dict[str, Any]
) -> None:
    from apcore.errors import ModuleError

    public = PublicExecutor(Executor.from_registry(strict_output_app.registry))
    with pytest.raises(ModuleError) as failure:
        await public.call_async("apflow.strict_output_probe", inputs)
    assert failure.value.code == "SCHEMA_VALIDATION_ERROR"
    assert failure.value.details["errors"][0]["path"] == (
        "/token/expires_in" if inputs["invalid"] is True else "/invalid"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["mcp", "mcp-trace", "mcp-stream", "a2a"])
async def test_mcp_errors_redact_credentials_and_preserve_error_contract(
    sdk_app: ApflowApp,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from apcore.errors import ModuleError
    from apflow.core.config_manager import get_config_manager

    secret = "synthetic-protocol-error-credential"
    monkeypatch.setenv("APFLOW_TEST_PROTOCOL_SECRET", secret)
    configuration = get_config_manager()
    monkeypatch.setattr(configuration, "_data", dict(configuration._data))
    configuration.set(
        "security.credentials", [{"id": "test", "env_var": "APFLOW_TEST_PROTOCOL_SECRET"}]
    )
    closed: list[bool] = []

    class FailingModule:
        description = "Return a typed upstream error."
        input_schema = output_schema = {"type": "object"}

        async def execute(self, inputs: dict[str, Any], context: Any) -> dict[str, Any]:
            raise ModuleError(
                code="UPSTREAM_FAILURE",
                message=f"Upstream failed: {secret}",
                details={"diagnostic": secret, "request_id": "request-123"},
                cause=ValueError(secret),
                retryable=True,
                user_fixable=False,
                ai_guidance=f"Inspect {secret}",
                suggestion=f"Retry {secret}",
            )

        async def stream(
            self, inputs: dict[str, Any], context: Any
        ) -> AsyncIterator[dict[str, Any]]:
            try:
                yield {"progress": 1}
                await self.execute(inputs, context)
            finally:
                closed.append(True)

    sdk_app.registry.register("apflow.failure_probe", FailingModule())
    with pytest.raises(ModuleError) as failure:
        await PublicExecutor(Executor.from_registry(sdk_app.registry)).call_async(
            "apflow.failure_probe", {}
        )
    assert failure.value.code == "UPSTREAM_FAILURE"
    assert failure.value.retryable is True and failure.value.user_fixable is False
    assert failure.value.details["request_id"] == "request-123"
    assert secret not in json.dumps(failure.value.to_dict())
    if protocol == "a2a":
        from apcore_a2a import async_serve

        server = await async_serve(PublicExecutor(Executor.from_registry(sdk_app.registry)))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server), base_url="http://test"
        ) as client:
            response = await client.post(
                "/",
                headers={"A2A-Version": "1.0"},
                json={
                    "jsonrpc": "2.0",
                    "id": "failure",
                    "method": "SendMessage",
                    "params": {
                        "message": {
                            "messageId": "failure",
                            "role": "ROLE_USER",
                            "parts": [{"data": {}}],
                            "metadata": {"skillId": "apflow.failure_probe"},
                        }
                    },
                },
            )
        assert response.status_code == 200
        assert "TASK_STATE_FAILED" in response.text
        serialized = response.text
    else:

        async def progress(progress: float, total: float | None, message: str | None) -> None:
            pass

        async with _mcp_client(sdk_app, trace=protocol == "mcp-trace") as client:
            result = await client.call_tool(
                "apflow.failure_probe",
                {},
                progress_callback=progress if protocol == "mcp-stream" else None,
            )
        assert result.isError
        serialized = result.model_dump_json()
        if protocol == "mcp-stream":
            assert closed == [True]
    if protocol != "a2a":
        assert "Upstream failed" in serialized
    assert secret not in serialized
    assert secret not in str([record.__dict__ for record in caplog.records])
    assert secret not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_public_protocol_stream_closes_upstream_immediately() -> None:
    closed: list[bool] = []

    async def chunks(*arguments: Any) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {"remaining_tokens": 10}
        finally:
            closed.append(True)

    native = SimpleNamespace(registry=Registry(), stream=chunks)
    stream = PublicExecutor(cast(Executor, native)).stream("apflow.probe", {})
    assert await anext(stream) == {"remaining_tokens": 10}
    await getattr(stream, "aclose")()
    assert closed == [True]


@asynccontextmanager
async def _management_client(app: ApflowApp, protocol: str) -> AsyncIterator[ModuleCall]:
    from apcore.approval import AutoApproveHandler
    from apcore_a2a import async_serve

    # These isolated synthetic reconciliation calls have explicit test approval.
    executor = Executor.from_registry(app.registry)
    executor.set_approval_handler(AutoApproveHandler())
    if protocol.startswith("mcp"):
        async with _mcp_client(app, executor, trace=protocol == "mcp-trace") as client:

            async def call(action: str, inputs: dict[str, Any]) -> dict[str, Any]:
                result = await client.call_tool(f"apflow.{action}", inputs)
                assert not result.isError, result.content
                assert result.structuredContent is not None
                return result.structuredContent

            yield call
    else:
        server = await async_serve(PublicExecutor(executor), name="apflow-management-test")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server), base_url="http://test"
        ) as client:

            async def call(action: str, inputs: dict[str, Any]) -> dict[str, Any]:
                request = {
                    "jsonrpc": "2.0",
                    "id": action,
                    "method": "SendMessage",
                    "params": {
                        "message": {
                            "messageId": action,
                            "role": "ROLE_USER",
                            "parts": [{"data": inputs}],
                            "metadata": {"skillId": f"apflow.{action}"},
                        }
                    },
                }
                response = await client.post("/", json=request, headers={"A2A-Version": "1.0"})
                assert response.status_code == 200, response.text
                body = response.json()
                assert "result" in body, body
                task = body["result"].get("task", body["result"])
                assert task["status"]["state"] == "TASK_STATE_COMPLETED", body
                return task["artifacts"][0]["parts"][0]["data"]

            yield call


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["mcp", "mcp-trace", "a2a"])
async def test_protocol_redacts_secrets_and_preserves_accounting_fields(
    sdk_app: ApflowApp, protocol: str
) -> None:
    class SensitiveModule:
        description = "Verify schema and credential redaction without hiding token counters."
        input_schema = {"type": "object"}
        output_schema = {
            "type": "object",
            "$defs": {"private": {"type": "string", "x-sensitive": True}},
            "properties": {
                "private": {"$ref": "#/$defs/private"},
                "actual_tokens": {"type": ["integer", "string"], "x-sensitive": True},
            },
        }

        async def execute(self, inputs: dict[str, Any], context: Any) -> dict[str, Any]:
            return {
                "private": "private-value",
                "api_key": "credential-value",
                "resetToken": "reset-value",
                "actual_tokens": 17,
                "embedded": {"token_usage": {"resetToken": 23}},
                "token_usage": {"total": 7},
                "remaining_tokens": 93,
            }

        async def stream(
            self, inputs: dict[str, Any], context: Any
        ) -> AsyncIterator[dict[str, Any]]:
            yield await self.execute(inputs, context)

    sdk_app.registry.register("apflow.protocol_redaction", SensitiveModule())
    async with _management_client(sdk_app, protocol) as call:
        result = await call("protocol_redaction", {})
    assert result["token_usage"] == {"total": 7}
    assert result["remaining_tokens"] == 93
    assert "private-value" not in json.dumps(result)
    assert "credential-value" not in json.dumps(result)
    assert "reset-value" not in json.dumps(result)
    assert result["actual_tokens"] != 17
    assert result["embedded"]["token_usage"] == "***REDACTED***"


@pytest.mark.asyncio
async def test_protocol_management_discovery_respects_a2a_public_approval_filter(
    sdk_app: ApflowApp,
) -> None:
    from apcore_a2a import async_serve

    expected = {
        f"apflow.{action}"
        for action in (
            "task.submit",
            "task.receipt",
            "task.reconcile",
            "run.get",
            "run.retry",
            "budget.scope",
            "budget.reconcile",
            "usage.events",
            "usage.ack",
        )
    }
    async with _mcp_client(sdk_app) as client:
        assert expected <= {tool.name for tool in (await client.list_tools()).tools}
    server = await async_serve(PublicExecutor(Executor.from_registry(sdk_app.registry)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://test"
    ) as client:
        response = await client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    visible = {skill["id"] for skill in response.json()["skills"]}
    assert expected - {"apflow.budget.reconcile"} <= visible
    assert "apflow.budget.reconcile" not in visible


@pytest.mark.asyncio
async def test_mcp_protocol_backend_preserves_sdk_approval_configuration(
    sdk_app: ApflowApp,
) -> None:
    from apcore_mcp import InMemoryApprovalStore

    runtime = ApflowMCP(
        sdk_app.registry, approval_store=InMemoryApprovalStore(), require_auth=False
    )
    server, _, _, _, _ = runtime._build_server_components()
    async with create_connected_server_and_client_session(server) as client:
        assert "__apcore_approval_check" in {
            tool.name for tool in (await client.list_tools()).tools
        }
        result = await client.call_tool(
            "apflow.budget.reconcile",
            {
                "scope": "unconfigured",
                "reservation_id": "never-started",
                "actual_tokens": 0,
                "evidence_ref": "receipt:pending",
            },
        )
    assert result.isError
    assert "approval pending" in json.dumps([item.model_dump() for item in result.content]).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["mcp", "a2a"])
async def test_protocol_budget_reconciliation_and_usage_ack_are_idempotent(
    sdk_app: ApflowApp, protocol: str
) -> None:
    from apflow.durability.attempts import AttemptRecorder
    from apflow.governance.ledger import BudgetLedger
    from apflow.governance.usage import UsageRecorder

    async with _management_client(sdk_app, protocol) as call:
        scope = await call("budget.scope", {"scope": "protocol-budget", "limit_tokens": 100})
        task = await sdk_app.task_repository.create_task(name="Unknown protocol receipt")
        recorder = AttemptRecorder(sdk_app.session)
        invocation = await recorder.start(task.id, 0, 1)
        ledger = BudgetLedger(sdk_app.session)
        await ledger.reserve(scope["scope_id"], task.id, invocation, 10)
        await recorder.finish(invocation, "unknown")
        inputs = {
            "scope": "protocol-budget",
            "reservation_id": invocation,
            "actual_tokens": 5,
            "evidence_ref": "receipt:protocol-test",
        }
        first = await call("budget.reconcile", inputs)
        assert await call("budget.reconcile", inputs) == first
        page = await call("usage.events", {"after_index": 0, "limit": 10})
        assert len(page["events"]) == 1
        event = page["events"][0]
        assert event["token_usage"]["total"] == 5
        acknowledgement = await call("usage.ack", {"event_id": event["id"]})
        assert await call("usage.ack", {"event_id": event["id"]}) == acknowledgement
        stored = await UsageRecorder(sdk_app.session).list_events(None)
        assert len(stored) == 1 and stored[0]["delivered_at"] is not None
        assert (await ledger.get_scope(scope["scope_id"])).spent_tokens == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["mcp", "a2a"])
async def test_protocol_retry_and_submission_preserve_original_run(
    sdk_app: ApflowApp, protocol: str
) -> None:
    from apflow.core.execution.runs import RunService

    task = await sdk_app.task_repository.create_task(name="Failed protocol run", status="failed")
    await RunService(sdk_app.session).ensure_run(task.id)
    async with _management_client(sdk_app, protocol) as call:
        before = await call("run.get", {"run_id": task.id})
        request = {"run_id": task.id, "idempotency_key": "protocol-retry-1"}
        receipt = await call("run.retry", request)
        assert await call("run.retry", request) == receipt
        assert receipt["run_id"] != task.id and receipt["retry_of_run_id"] == task.id
        assert await call("run.get", {"run_id": task.id}) == before
        created = await call(
            "task.create", {"name": "Protocol draft", "idempotency_key": "protocol-create-1"}
        )
        recovered = await call("task.receipt", {"idempotency_key": "protocol-create-1"})
        assert recovered["id"] == created["id"]
        submitted = await call("task.submit", {"task_id": created["id"]})
        assert await call("task.submit", {"task_id": created["id"]}) == submitted


@pytest.mark.asyncio
async def test_mcp_task_crud_returns_valid_structured_output(sdk_app: ApflowApp) -> None:
    async with _mcp_client(sdk_app) as client:
        listed = await client.list_tools()
        create_tool = next(tool for tool in listed.tools if tool.name == "apflow.task.create")
        assert create_tool.outputSchema is not None
        assert create_tool.inputSchema["required"] == ["name"]
        assert create_tool.inputSchema["properties"]["priority"]["type"] == "integer"
        inputs = dict.fromkeys(create_tool.inputSchema.get("required", []))
        inputs["name"] = "SDK compatibility"
        inputs["inputs"] = {"payload": {"value": None}}
        created = await client.call_tool("apflow.task.create", inputs)
        assert not created.isError, created.content
        assert created.structuredContent is not None
        task_id = created.structuredContent["id"]
        assert created.structuredContent["created_at"]
        fetched = await client.call_tool("apflow.task.get", {"task_id": task_id})
        assert not fetched.isError, fetched.content
        assert fetched.structuredContent is not None
        assert fetched.structuredContent["name"] == "SDK compatibility"
        assert fetched.structuredContent["inputs"] == {"payload": {"value": None}}
        list_tool = next(tool for tool in listed.tools if tool.name == "apflow.task.list")
        tasks = await client.call_tool(
            "apflow.task.list", dict.fromkeys(list_tool.inputSchema.get("required", []))
        )
        assert not tasks.isError, tasks.content
        assert tasks.structuredContent is not None
        assert tasks.structuredContent["total"] == 1


@pytest.mark.asyncio
async def test_mcp_executor_output_redacts_sensitive_local_references(sdk_app: ApflowApp) -> None:
    class SensitiveExecutor:
        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            return {"value": "sdk-secret-value"}

    adapter = ExecutableTaskModuleAdapter(
        executor_class=SensitiveExecutor,
        executor_id="sensitive",
        executor_name="Sensitive output",
        executor_description="Return a sensitive value for redaction verification",
        input_schema={"type": "object"},
        output_schema={
            "type": "object",
            "$defs": {"value": {"type": "string", "x-sensitive": True}},
            "properties": {"value": {"$ref": "#/$defs/value"}},
            "required": ["value"],
        },
    )
    sdk_app.registry.register("apflow.sensitive", adapter)
    async with _mcp_client(sdk_app) as client:
        result = await client.call_tool("apflow.sensitive", {})
    assert not result.isError, result.content
    assert result.structuredContent is not None
    assert result.structuredContent["value"] != "sdk-secret-value"
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert "sdk-secret-value" not in content.text
    assert json.loads(content.text) == result.structuredContent


@pytest.mark.asyncio
async def test_mcp_create_tree_accepts_omitted_optional_fields(sdk_app: ApflowApp) -> None:
    async with _mcp_client(sdk_app) as client:
        tools = (await client.list_tools()).tools
        tool = next(tool for tool in tools if tool.name == "apflow.task.create_tree")
        fields = tool.inputSchema["properties"]["tasks"]["items"]["required"]
        assert fields == ["name"]
        root = {**dict.fromkeys(fields), "name": "root"}
        child = {**dict.fromkeys(fields), "name": "child", "parent_id": "root"}
        result = await client.call_tool("apflow.task.create_tree", {"tasks": [root, child]})
    assert not result.isError, result.content
    assert result.structuredContent is not None
    assert result.structuredContent["task_count"] == 2


def _list_tasks_request() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "sdk-test",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "sdk-message",
                "role": "ROLE_USER",
                "parts": [{"data": {}}],
                "metadata": {"skillId": "apflow.task.list"},
            }
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_id", "arguments"),
    [
        ("apflow.task.create", {"name": "invalid", "priority": None}),
        ("apflow.task.create_tree", {"tasks": [{"name": "invalid", "priority": None}]}),
        ("apflow.task.list", {"status": None}),
    ],
)
async def test_mcp_wire_rejects_optional_nonnullable_values(
    sdk_app: ApflowApp, module_id: str, arguments: dict[str, Any]
) -> None:
    async with _mcp_client(sdk_app) as client:
        result = await client.call_tool(module_id, arguments)
        assert result.isError, result.content
        tasks = await client.call_tool("apflow.task.list", {})
    assert not tasks.isError, tasks.content
    assert tasks.structuredContent is not None
    assert tasks.structuredContent["total"] == 0


@pytest.mark.asyncio
async def test_combined_a2a_executes_task_module(
    sdk_app: ApflowApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses: dict[str, Any] = {}

    class InProcessServer:
        def __init__(self, config: Any) -> None:
            self.app = config.app

        async def serve(self) -> None:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://test"
            ) as client:
                card = await client.get("/a2a/.well-known/agent-card.json")
                responses["card"] = card.json()
                response = await client.post(
                    "/a2a/",
                    headers={"A2A-Version": "1.0"},
                    json=_list_tasks_request(),
                )
                assert response.status_code == 200, response.text
                responses["execution"] = response.json()

    monkeypatch.setattr("uvicorn.Server", InProcessServer)
    await _serve_all_async(
        sdk_app.registry,
        host="127.0.0.1",
        port=9999,
        title="apflow-test",
        version="1.0",
        description="SDK compatibility",
        cors_origins=None,
        log_level=None,
        explorer=False,
        metrics=False,
        sys_modules=False,
        push_notifications=False,
        webhook=False,
        webhook_secret=None,
        auth=False,
        scheduler=False,
    )
    assert any(skill["id"] == "apflow.task.list" for skill in responses["card"]["skills"])
    assert "result" in responses["execution"], responses["execution"]
    result = responses["execution"]["result"]
    task = result.get("task", result)
    assert task["status"]["state"] == "TASK_STATE_COMPLETED", responses["execution"]
    assert task["artifacts"][0]["parts"][0]["data"] == {"tasks": [], "total": 0}


@pytest.mark.parametrize("protocol", ["a2a", "mcp"])
def test_cli_protocol_servers_receive_working_executors(
    sdk_app: ApflowApp, monkeypatch: pytest.MonkeyPatch, protocol: str
) -> None:
    outputs = []

    def capture_serve(backend: Registry | Executor | PublicExecutor, **kwargs: Any) -> None:
        assert "middleware" not in kwargs
        executor = Executor.from_registry(backend) if isinstance(backend, Registry) else backend
        assert executor.registry is sdk_app.registry
        outputs.append(asyncio.run(executor.call_async("apflow.task.list", {})))

    monkeypatch.setattr("apflow.app.create_app", lambda **_kwargs: sdk_app)
    if protocol == "a2a":
        monkeypatch.setattr("apcore_a2a.serve", capture_serve)
    else:
        monkeypatch.setattr(
            "apflow.bridge.protocol.ApflowMCP.serve",
            lambda self, **kwargs: capture_serve(self.executor, **kwargs),
        )
    result = CliRunner().invoke(serve if protocol == "a2a" else mcp)
    assert result.exit_code == 0, result.output
    assert outputs == [{"tasks": [], "total": 0}]


def test_generated_cli_task_commands_preserve_defaults_and_payload_nulls(
    sdk_app: ApflowApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("apflow.app.create_app", lambda **_kwargs: sdk_app)
    command = _build_cli()
    runner = CliRunner()
    created = runner.invoke(
        command,
        [
            "task",
            "create",
            "--name",
            "SDK CLI compatibility",
            "--input",
            "-",
            "--format",
            "json",
        ],
        input='{"inputs":{"payload":{"value":null}}}',
    )
    assert created.exit_code == 0, created.output
    task_id = json.loads(created.stdout)["id"]
    fetched = runner.invoke(command, ["task", "get", "--task-id", task_id, "--format", "json"])
    assert fetched.exit_code == 0, fetched.output
    assert json.loads(fetched.stdout)["inputs"] == {"payload": {"value": None}}
    listed = runner.invoke(command, ["task", "list", "--format", "json"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.stdout)["total"] == 1
