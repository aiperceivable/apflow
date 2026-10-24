"""MCP output contracts describe public values while native schemas stay strict."""

from copy import deepcopy
from typing import Any

import pytest
from apcore import Context, Registry
from jsonschema.validators import validator_for

from apflow.bridge.protocol import ApflowMCP
from apflow.core.config_manager import get_config_manager


def _public_contract(
    schema: dict[str, Any], value: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    class OutputModule:
        description = "Return schema-constrained public data"
        input_schema = {"type": "object"}
        output_schema = schema

        async def execute(self, inputs: dict[str, Any], context: Context) -> dict[str, Any]:
            return value

    validator_for(schema)(schema).validate(value)
    original = deepcopy(schema)
    registry = Registry()
    registry.register("output", OutputModule())
    runtime = ApflowMCP(registry, async_tasks=False)
    tool = runtime._build_server_components()[2][0]
    public = runtime.executor._redact("output", value)
    assert schema == original
    assert runtime.registry is registry
    assert tool.outputSchema is not None
    validator_for(tool.outputSchema).check_schema(tool.outputSchema)
    return tool.outputSchema, public


@pytest.mark.parametrize(
    "schema,value",
    [
        (
            {"type": "object", "properties": {"private": {"type": "integer", "x-sensitive": True}}},
            {"private": 42},
        ),
        (
            {
                "type": "object",
                "$defs": {
                    "private": {"$anchor": "private", "type": "integer", "x-sensitive": True}
                },
                "properties": {"private": {"$ref": "#private"}},
            },
            {"private": 42},
        ),
        (
            {
                "type": "object",
                "properties": {
                    "private": {"allOf": [{"type": "integer", "minimum": 1}, {"x-sensitive": True}]}
                },
            },
            {"private": 42},
        ),
        (
            {
                "type": "object",
                "properties": {
                    "entries": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": {
                            "oneOf": [
                                {
                                    "type": "object",
                                    "properties": {"value": {"const": number, "x-sensitive": True}},
                                }
                                for number in (1, 2)
                            ]
                        },
                    }
                },
            },
            {"entries": [{"value": 1}, {"value": 2}]},
        ),
        (
            {
                "type": "object",
                "properties": {
                    "choice": {
                        "oneOf": [{"type": "integer", "x-sensitive": True}, {"type": "array"}]
                    },
                    "alias": {"$ref": "#/properties/choice/oneOf/0"},
                },
            },
            {"choice": 42, "alias": 42},
        ),
        (
            {
                "type": "object",
                "$defs": {
                    "entry": {
                        "$anchor": "entry",
                        "type": "object",
                        "properties": {
                            "private": {"type": "integer", "x-sensitive": True},
                            "next": {"anyOf": [{"$ref": "#entry"}, {"type": "null"}]},
                        },
                    }
                },
                "properties": {"first": {"$ref": "#entry"}},
            },
            {"first": {"private": 1, "next": {"private": 2, "next": None}}},
        ),
        (
            {
                "type": "object",
                "$defs": {
                    "rule": {
                        "if": {"properties": {"value": {"type": "integer", "x-sensitive": True}}}
                    }
                },
                "properties": {"private": {"$ref": "#/$defs/rule/if/properties/value"}},
            },
            {"private": 42},
        ),
        (
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "definitions": {"value": {"type": "integer", "x-sensitive": True}},
                "properties": {"private": {"$ref": "#/definitions/value", "type": "boolean"}},
            },
            {"private": 42},
        ),
        (
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": {
                    "values": {
                        "type": "array",
                        "items": [{"type": "integer", "x-sensitive": True}, {"type": "boolean"}],
                        "additionalItems": False,
                    }
                },
            },
            {"values": [42, False]},
        ),
    ],
)
def test_mcp_public_schema_accepts_redaction_through_schema_references_and_composition(
    schema: dict[str, Any], value: dict[str, Any]
) -> None:
    public_schema, public = _public_contract(schema, value)
    assert public != value
    validator_for(public_schema)(public_schema).validate(public)


def test_mcp_public_schema_accepts_credential_replacement_in_constrained_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APFLOW_TEST_SCHEMA_SECRET", "test-key")
    configuration = get_config_manager()
    monkeypatch.setattr(configuration, "_data", dict(configuration._data))
    configuration.set(
        "security.credentials", [{"id": "test", "env_var": "APFLOW_TEST_SCHEMA_SECRET"}]
    )
    value = "prefix-test-key"
    schema = {
        "type": "object",
        "properties": {
            "receipt": {
                "type": "string",
                "enum": [value],
                "maxLength": len(value),
                "pattern": "^prefix-test-key$",
            }
        },
        "required": ["receipt"],
    }
    public_schema, public = _public_contract(schema, {"receipt": value})
    assert public == {"receipt": "prefix-***REDACTED***"}
    validator_for(public_schema)(public_schema).validate(public)


def test_mcp_public_schema_retains_structure_numeric_bounds_and_nulls() -> None:
    schema = {
        "type": "object",
        "properties": {
            "private": {"type": ["integer", "null"], "x-sensitive": True},
            "score": {"type": "integer", "minimum": 0},
            "items": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": "boolean"}},
        },
        "required": ["private", "score", "items"],
        "additionalProperties": False,
    }
    public_schema, public = _public_contract(
        schema, {"private": None, "score": 2, "items": [False]}
    )
    validator = validator_for(public_schema)(public_schema)
    validator.validate(public)
    for invalid in (
        {},
        {**public, "score": -1},
        {**public, "items": []},
        {**public, "extra": 1},
        {**public, "score": []},
    ):
        assert not validator.is_valid(invalid)


def test_mcp_public_schema_resolves_embedded_resources_in_their_own_scope() -> None:
    schema = {
        "$id": "urn:apflow:output",
        "type": "object",
        "$defs": {
            "child": {
                "$id": "urn:apflow:child",
                "type": "object",
                "$defs": {
                    "private": {"$anchor": "private", "type": "integer", "x-sensitive": True}
                },
                "properties": {"private": {"$ref": "#private"}},
            }
        },
        "properties": {"child": {"$ref": "urn:apflow:child"}},
    }
    public_schema, public = _public_contract(schema, {"child": {"private": 42}})
    assert public == {"child": {"private": "***REDACTED***"}}
    validator_for(public_schema)(public_schema).validate(public)


def test_mcp_public_schema_rejects_unresolved_references_without_retrieving_them() -> None:
    from apflow.bridge.output_schema import public_output_schema

    with pytest.raises(ValueError, match="unresolved reference"):
        public_output_schema({"type": "object", "properties": {"value": {"$ref": "#/missing"}}})


def test_mcp_public_schema_keeps_shared_python_fragments_in_distinct_reference_scopes() -> None:
    shared = {"$ref": "#value"}
    schema = {
        "type": "object",
        "$defs": {
            name: {
                "$id": "urn:apflow:" + name,
                "type": "object",
                "$defs": {"value": {"$anchor": "value", "type": kind}},
                "properties": {
                    "value": shared,
                    "private": {"type": "integer", "x-sensitive": True},
                },
            }
            for name, kind in (("integer", "integer"), ("array", "array"))
        },
        "properties": {
            "integer": {"$ref": "urn:apflow:integer"},
            "array": {"$ref": "urn:apflow:array"},
        },
    }
    value = {"integer": {"value": 1, "private": 2}, "array": {"value": [], "private": 3}}
    public_schema, public = _public_contract(schema, value)
    validator_for(public_schema)(public_schema).validate(public)
