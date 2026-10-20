"""Exercise apflow modules through the installed protocol SDKs."""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from apcore import Executor, Registry
from apcore_mcp import ExecutionRouter, MCPServerFactory
from click.testing import CliRunner
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent
from sqlalchemy.orm import Session
from sqlalchemy.engine import Engine

from apflow.api.combined import _serve_all_async
from apflow.app import ApflowApp, create_app
from apflow.bridge.module_adapter import ExecutableTaskModuleAdapter
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
async def _mcp_client(app: ApflowApp) -> AsyncIterator[ClientSession]:
    factory = MCPServerFactory()
    server = factory.create_server(name="apflow-test")
    tools = factory.build_tools(app.registry)
    schemas = {}
    for module_id in app.registry.list():
        definition = app.registry.get_definition(module_id)
        assert definition is not None
        schemas[module_id] = definition.output_schema
    executor = Executor.from_registry(app.registry)
    router = ExecutionRouter(executor, output_schema_map=schemas)
    factory.register_handlers(server, tools, router)
    async with create_connected_server_and_client_session(server) as client:
        yield client


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

    def capture_serve(backend: Registry | Executor, **kwargs: Any) -> None:
        assert isinstance(backend, Registry)
        assert "middleware" not in kwargs
        executor = Executor.from_registry(backend)
        assert executor.registry is sdk_app.registry
        outputs.append(asyncio.run(executor.call_async("apflow.task.list", {})))

    monkeypatch.setattr("apflow.app.create_app", lambda **_kwargs: sdk_app)
    monkeypatch.setattr(f"apcore_{protocol}.serve", capture_serve)
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
