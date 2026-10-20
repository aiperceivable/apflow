"""MCP discovery and execution preserve native module input semantics."""

import json
from copy import deepcopy
from typing import Any

import pytest
from apcore import Context, Executor, Registry
from apcore_mcp import ExecutionRouter, MCPServerFactory
from jsonschema import Draft202012Validator


class EchoInputsModule:
    description = "Return the original inputs with an omitted enabled default"
    input_schema = {
        "type": "object",
        "$defs": {"integer": {"type": "integer"}},
        "properties": {
            "name": {"type": "string"},
            "enabled": {"type": "boolean", "default": True},
            "limit": {"$ref": "#/$defs/integer"},
            "nullable": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "status": {"type": "string", "enum": ["pending", "done"]},
            "payload": {"type": "object"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "parent": {"type": "string"}},
                    "required": ["name"],
                },
            },
        },
        "required": ["name"],
    }
    output_schema = {"type": "object"}

    async def execute(self, inputs: dict[str, Any], context: Context) -> dict[str, Any]:
        return {**inputs, "enabled": inputs.get("enabled", True)}


@pytest.fixture
def registry() -> Registry:
    registry = Registry()
    registry.register("echo", EchoInputsModule())
    return registry


def test_mcp_schema_preserves_optional_fields_defaults_and_references(registry: Registry) -> None:
    original = deepcopy(EchoInputsModule.input_schema)
    tool = MCPServerFactory().build_tools(registry)[0]
    assert tool.inputSchema == original
    assert tool.inputSchema["required"] == ["name"]
    assert tool.inputSchema["properties"]["enabled"]["default"] is True
    assert tool.inputSchema["properties"]["limit"] == {"$ref": "#/$defs/integer"}
    assert "additionalProperties" not in tool.inputSchema
    assert EchoInputsModule.input_schema == original


def test_core_strict_optional_enum_accepts_null_without_widening_runtime(
    registry: Registry,
) -> None:
    exported = registry.export_schema("echo", strict=True)
    assert exported is not None
    strict_schema = exported["input_schema"]
    placeholders: dict[str, Any] = dict.fromkeys(strict_schema["properties"])
    placeholders["name"] = "root"
    assert Draft202012Validator(strict_schema).is_valid(placeholders)
    assert (
        not Executor.from_registry(registry)
        .validate("echo", {"name": "root", "status": None})
        .valid
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("validate_inputs", [False, True])
async def test_mcp_keeps_valid_nulls_false_values_and_omitted_defaults(
    registry: Registry, validate_inputs: bool
) -> None:
    inputs = {
        "name": "root",
        "enabled": False,
        "nullable": None,
        "payload": {"value": None},
        "items": [{"name": "child"}],
        "extra": None,
    }
    before = deepcopy(inputs)
    router = ExecutionRouter(
        Executor.from_registry(registry),
        validate_inputs=validate_inputs,
        output_schema_map={"echo": EchoInputsModule.output_schema},
    )
    content, is_error, _ = await router.handle_call("echo", inputs)
    assert not is_error
    assert json.loads(content[0]["text"]) == inputs
    content, is_error, _ = await router.handle_call("echo", {"name": "root"})
    assert not is_error
    assert json.loads(content[0]["text"]) == {"name": "root", "enabled": True}
    assert inputs == before


@pytest.mark.asyncio
@pytest.mark.parametrize("validate_inputs", [False, True])
@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"enabled": None},
        {"limit": None},
        {"status": None},
        {"items": [{"name": "child", "parent": None}]},
        {"name": None},
    ],
)
async def test_mcp_rejects_invalid_nulls_instead_of_omitting_them(
    registry: Registry, validate_inputs: bool, invalid_fields: dict[str, Any]
) -> None:
    inputs = {"name": "root", **invalid_fields}
    before = deepcopy(inputs)
    tool = MCPServerFactory().build_tools(registry)[0]
    assert not Draft202012Validator(tool.inputSchema).is_valid(inputs)
    executor = Executor.from_registry(registry)
    assert not executor.validate("echo", inputs).valid
    router = ExecutionRouter(executor, validate_inputs=validate_inputs)
    _, is_error, _ = await router.handle_call("echo", inputs)
    assert is_error
    assert inputs == before
