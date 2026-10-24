"""Explicit executor return formats and the canonical execution envelope."""

from copy import deepcopy
from typing import Any, Literal, NotRequired, TypedDict

from apflow.core.execution.errors import ValidationError
from apflow.core.execution.error_info import ERROR_INFO_SCHEMA, ErrorInfo, validate_error_info

ResultFormat = Literal["legacy", "raw", "envelope"]


class ExecutorResult(TypedDict):
    """Business output and execution metadata, interpreted exactly once."""

    result: Any
    error: NotRequired[str | None]
    token_usage: NotRequired[dict[str, int]]
    error_info: NotRequired[ErrorInfo]


def normalize_executor_result(value: Any, result_format: ResultFormat) -> ExecutorResult:
    """Adapt a declared return format; never infer a format from business keys.

    Legacy executors retain their entire return value and top-level error/usage
    conventions. Raw returns have no reserved business keys. Envelope executors
    must put every business field inside result.
    """
    if result_format not in ("legacy", "raw", "envelope"):
        raise ValidationError(f"Unknown executor result format: {result_format}")
    if result_format == "raw":
        return {"result": deepcopy(value)}
    if result_format == "legacy":
        outcome: ExecutorResult = {"result": deepcopy(value)}
        if isinstance(value, dict):
            if value.get("error"):
                outcome["error"] = str(value["error"])
            if value.get("token_usage"):
                usage = deepcopy(value["token_usage"])
                usage.setdefault("total", usage.get("input", 0) + usage.get("output", 0))
                outcome["token_usage"] = usage
            if value.get("error_info"):
                outcome["error_info"] = _validate_error_info(value["error_info"])
        return outcome
    if not isinstance(value, dict):
        raise ValidationError("An envelope executor must return a dictionary")
    unexpected = set(value) - {"result", "error", "token_usage", "error_info"}
    if unexpected:
        raise ValidationError(
            f"Unexpected execution envelope fields: {sorted(unexpected, key=str)}. "
            "Put business fields inside result."
        )
    error = value.get("error")
    if error is not None and (not isinstance(error, str) or not error):
        raise ValidationError("Execution envelope error must be a non-empty string or null")
    if "result" not in value and error is None:
        raise ValidationError("A successful execution envelope requires result, including null")
    outcome = {"result": deepcopy(value.get("result"))}
    if "error" in value:
        outcome["error"] = error
    if "token_usage" in value:
        outcome["token_usage"] = _validate_token_usage(value["token_usage"])
    if "error_info" in value:
        if error is None:
            raise ValidationError("error_info requires an execution error")
        outcome["error_info"] = _validate_error_info(value["error_info"])
    return outcome


def _validate_error_info(value: Any) -> ErrorInfo:
    try:
        return validate_error_info(deepcopy(value))
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _validate_token_usage(value: Any) -> dict[str, int]:
    """Validate counters before they reach atomic usage accumulation."""
    if not isinstance(value, dict) or set(value) - {"input", "output", "total"}:
        raise ValidationError("token_usage must contain only input, output, and total counters")
    if any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in value.values()
    ):
        raise ValidationError("token_usage counters must be non-negative integers")
    usage = dict(value)
    usage.setdefault("total", usage.get("input", 0) + usage.get("output", 0))
    return usage


def executor_result_schema(business_schema: dict[str, Any]) -> dict[str, Any]:
    """Describe the actual envelope returned by an executor or module."""
    business = _relocate_schema_refs(business_schema)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "result": {"$ref": "#/$defs/business_result"} if business else {},
            "error": {"type": ["string", "null"], "minLength": 1},
            "error_info": deepcopy(ERROR_INFO_SCHEMA),
            "token_usage": {
                "type": "object",
                "properties": {
                    key: {"type": "integer", "minimum": 0} for key in ("input", "output", "total")
                },
                "additionalProperties": False,
            },
        },
        "anyOf": [
            {"required": ["result"]},
            {"required": ["error"], "properties": {"error": {"type": "string"}}},
        ],
        "additionalProperties": False,
        "dependentSchemas": {
            "error_info": {"required": ["error"], "properties": {"error": {"type": "string"}}}
        },
    }
    if business:
        schema["$defs"] = {"business_result": {"anyOf": [business, {"type": "null"}]}}
        # Only failures may replace a declared business value with null.
        schema["if"] = {"required": ["error"], "properties": {"error": {"type": "string"}}}
        schema["else"] = {"properties": {"result": {"$ref": "#/$defs/business_result/anyOf/0"}}}
    return schema


def _relocate_schema_refs(node: Any) -> Any:
    """Move schema pointers while preserving literal defaults and nested resources."""
    if isinstance(node, list):
        return [_relocate_schema_refs(item) for item in node]
    if not isinstance(node, dict):
        return node
    relocated = deepcopy(node)
    if node.get("$id"):
        return relocated
    for key in ("$ref", "$dynamicRef"):
        reference = node.get(key)
        if isinstance(reference, str) and (reference == "#" or reference.startswith("#/")):
            relocated[key] = "#/$defs/business_result/anyOf/0" + reference[1:]
    for key in (
        "properties",
        "patternProperties",
        "$defs",
        "definitions",
        "dependentSchemas",
        "dependencies",
    ):
        if isinstance(node.get(key), dict):
            relocated[key] = {
                name: (
                    _relocate_schema_refs(schema) if isinstance(schema, dict) else deepcopy(schema)
                )
                for name, schema in node[key].items()
            }
    for key in (
        "allOf",
        "anyOf",
        "oneOf",
        "prefixItems",
        "items",
        "additionalItems",
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "propertyNames",
        "contentSchema",
    ):
        if key in node:
            relocated[key] = _relocate_schema_refs(node[key])
    return relocated
