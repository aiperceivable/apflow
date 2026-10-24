"""Unit contracts for result normalization and standalone envelope schemas."""

from copy import deepcopy
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

from apflow.core.execution.errors import ValidationError
from apflow.core.execution.error_info import http_error_info
from apflow.core.execution.result_protocol import (
    ResultFormat,
    executor_result_schema,
    normalize_executor_result,
)


@pytest.mark.parametrize("result_format", ["raw", "legacy", "envelope"])
@pytest.mark.parametrize("payload", [None, False, 0, "", [], {}, {"result": {"error": "data"}}])
def test_normalization_preserves_business_values(result_format: ResultFormat, payload: Any) -> None:
    value = {"result": payload} if result_format == "envelope" else payload
    assert normalize_executor_result(value, result_format) == {"result": payload}


@pytest.mark.parametrize("result_format", ["raw", "legacy", "envelope"])
def test_normalization_detaches_nested_business_values(result_format: ResultFormat) -> None:
    payload = {"items": [{"name": "original"}]}
    value = {"result": payload} if result_format == "envelope" else payload
    normalized = normalize_executor_result(value, result_format)
    normalized["result"]["items"][0]["name"] = "changed"
    assert payload == {"items": [{"name": "original"}]}
    payload["items"].append({"name": "later"})
    assert len(normalized["result"]["items"]) == 1


def test_raw_reserved_names_are_only_business_data() -> None:
    value = {"result": 1, "error": "business", "token_usage": {"total": -1}}
    assert normalize_executor_result(value, "raw") == {"result": value}


def test_legacy_preserves_payload_and_historical_metadata() -> None:
    value = {"result": 1, "error": 42, "token_usage": {"total": 7, "provider": 2}}
    normalized = normalize_executor_result(value, "legacy")
    assert normalized == {"result": value, "error": "42", "token_usage": value["token_usage"]}
    assert "token_usage" in normalized
    normalized["token_usage"]["total"] = 9
    assert value["token_usage"]["total"] == 7
    assert normalized["result"]["token_usage"]["total"] == 7


@pytest.mark.parametrize("empty", [None, False, 0, "", {}, []])
def test_legacy_ignores_falsy_metadata_without_discarding_it(empty: Any) -> None:
    value = {"error": empty, "token_usage": empty}
    assert normalize_executor_result(value, "legacy") == {"result": value}


@pytest.mark.parametrize("result_format", ["", "auto", None, 1])
def test_unknown_format_raises_validation_error(result_format: Any) -> None:
    with pytest.raises(ValidationError, match="Unknown executor result format"):
        normalize_executor_result({}, cast(ResultFormat, result_format))


@pytest.mark.parametrize("value", [None, False, 1, "", [], ()])
def test_envelope_requires_dictionary(value: Any) -> None:
    with pytest.raises(ValidationError, match="must return a dictionary"):
        normalize_executor_result(value, "envelope")


@pytest.mark.parametrize("value", [{}, {"error": None}, {"token_usage": {}}])
def test_success_requires_explicit_result(value: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="requires result"):
        normalize_executor_result(value, "envelope")


@pytest.mark.parametrize("error", ["", False, True, 0, 1, [], {}])
def test_envelope_rejects_invalid_error(error: Any) -> None:
    with pytest.raises(ValidationError, match="error must be"):
        normalize_executor_result({"result": None, "error": error}, "envelope")


@pytest.mark.parametrize("value", [{"error": "Failed"}, {"result": False, "error": "Failed"}])
def test_failure_can_omit_result_or_keep_partial_output(value: dict[str, Any]) -> None:
    assert normalize_executor_result(value, "envelope") == {
        "result": value.get("result"),
        "error": "Failed",
    }


def test_null_error_is_preserved_as_success() -> None:
    assert normalize_executor_result({"result": 0, "error": None}, "envelope") == {
        "result": 0,
        "error": None,
    }


def test_error_info_preserves_versioned_metadata_without_mutating_source() -> None:
    info = http_error_info(503, "GET", upstream_code="WORLD_UNAVAILABLE", request_id="r-1")
    value = {"result": {"partial": True}, "error": "Unavailable", "error_info": info}
    result = normalize_executor_result(value, "envelope")
    assert result == value
    assert Draft202012Validator(executor_result_schema({})).is_valid(value)
    copied_info = result.get("error_info")
    assert copied_info is not None
    copied_info["code"] = "OTHER"
    assert info["code"] == "HTTP_503"


@pytest.mark.parametrize("info", [{"version": 2}, {"version": 1}, "error", None])
def test_error_info_rejects_invalid_or_unknown_versions(info: Any) -> None:
    value = {"result": None, "error": "Failure", "error_info": info}
    with pytest.raises(ValidationError, match="error_info"):
        normalize_executor_result(value, "envelope")
    assert not Draft202012Validator(executor_result_schema({})).is_valid(value)


def test_envelope_rejects_outer_business_fields() -> None:
    with pytest.raises(ValidationError, match="Put business fields inside result"):
        normalize_executor_result({"result": 1, "evidence": []}, "envelope")


def test_envelope_reports_mixed_type_unexpected_keys_as_validation_error() -> None:
    with pytest.raises(ValidationError, match="Unexpected execution envelope fields"):
        normalize_executor_result({"result": 1, "evidence": [], 2: "invalid key"}, "envelope")


@pytest.mark.parametrize("usage", [None, [], 1, "1", {"cached": 1}])
def test_token_usage_requires_known_counter_object(usage: Any) -> None:
    with pytest.raises(ValidationError, match="only input, output, and total"):
        normalize_executor_result({"result": None, "token_usage": usage}, "envelope")


@pytest.mark.parametrize("counter", [None, False, True, -1, 1.5, "1", [], {}])
@pytest.mark.parametrize("key", ["input", "output", "total"])
def test_token_usage_rejects_invalid_counters(key: str, counter: Any) -> None:
    with pytest.raises(ValidationError, match="non-negative integers"):
        normalize_executor_result({"result": None, "token_usage": {key: counter}}, "envelope")


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({}, {"total": 0}),
        ({"input": 3}, {"input": 3, "total": 3}),
        ({"output": 2}, {"output": 2, "total": 2}),
        ({"input": 3, "output": 2}, {"input": 3, "output": 2, "total": 5}),
        ({"input": 3, "output": 2, "total": 0}, {"input": 3, "output": 2, "total": 0}),
        ({"total": 10**30}, {"total": 10**30}),
    ],
)
def test_token_usage_derives_only_missing_total_and_copies_counters(
    usage: dict[str, int], expected: dict[str, int]
) -> None:
    before = dict(usage)
    normalized = normalize_executor_result({"result": None, "token_usage": usage}, "envelope")
    assert "token_usage" in normalized
    assert normalized["token_usage"] == expected
    assert usage == before
    normalized["token_usage"]["total"] = 9
    assert usage == before


@pytest.mark.parametrize(
    "frame,valid",
    [
        ({"result": None}, True),
        ({"result": False}, True),
        ({"result": 0, "error": None}, True),
        ({"result": [], "error": "Failed"}, True),
        ({"error": "Failed"}, True),
        ({"error": "Failed", "token_usage": {}}, True),
        ({}, False),
        ({"error": None}, False),
        ({"error": ""}, False),
        ({"result": 1, "error": False}, False),
        ({"result": 1, "evidence": []}, False),
        ({"result": 1, "token_usage": None}, False),
        ({"result": 1, "token_usage": {"extra": 0}}, False),
        ({"result": 1, "token_usage": {"input": -1}}, False),
        ({"result": 1, "token_usage": {"output": True}}, False),
        ({"result": 1, "token_usage": {"input": 3, "output": 2}}, True),
    ],
)
def test_default_schema_matches_envelope_wire_contract(frame: dict[str, Any], valid: bool) -> None:
    schema = executor_result_schema({})
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(frame) is valid


def test_business_schema_validates_nested_result_and_keeps_failure_null() -> None:
    schema = executor_result_schema({"type": "integer", "minimum": 1})
    validator = Draft202012Validator(schema)
    assert validator.is_valid({"result": 2})
    assert validator.is_valid({"result": None, "error": "Failed"})
    assert not validator.is_valid({"result": "2"})
    assert not validator.is_valid({"result": 0})
    assert not validator.is_valid({"result": None})
    assert not validator.is_valid({"result": None, "error": None})


def test_explicit_nullable_business_schema_accepts_successful_null() -> None:
    validator = Draft202012Validator(executor_result_schema({"type": ["integer", "null"]}))
    assert validator.is_valid({"result": None})
    assert validator.is_valid({"result": None, "error": None})


@pytest.mark.parametrize(
    "anchor_keyword,reference_keyword", [("$anchor", "$ref"), ("$dynamicAnchor", "$dynamicRef")]
)
def test_named_anchors_keep_recursive_business_scope(
    anchor_keyword: str, reference_keyword: str
) -> None:
    business = {
        anchor_keyword: "node",
        "type": "object",
        "properties": {"child": {reference_keyword: "#node"}},
    }
    validator = Draft202012Validator(executor_result_schema(business))
    assert validator.is_valid({"result": {"child": {"child": {}}}})
    assert not validator.is_valid({"result": {"child": 1}})


def test_schema_creation_preserves_annotations_and_has_no_shared_mutable_state() -> None:
    literal = {"$ref": "#/literal", "$dynamicRef": "#", "properties": {"value": 1}}
    business = {
        "type": "object",
        "default": literal,
        "examples": [literal],
        "enum": [literal],
        "properties": {"secret": {"type": "string", "x-sensitive": True}},
    }
    original = deepcopy(business)
    schema = executor_result_schema(business)
    wrapped = schema["$defs"]["business_result"]["anyOf"][0]
    assert wrapped == original
    wrapped["default"]["$ref"] = "changed"
    wrapped["properties"]["secret"]["x-sensitive"] = False
    assert business == original
    assert executor_result_schema(business)["$defs"]["business_result"]["anyOf"][0] == original


@pytest.mark.parametrize(
    "keyword",
    ["properties", "patternProperties", "$defs", "definitions", "dependentSchemas", "dependencies"],
)
def test_schema_map_positions_relocate_refs_and_preserve_non_schema_values(keyword: str) -> None:
    business = {keyword: {"schema": {"$ref": "#/target"}, "boolean": False, "fields": ["name"]}}
    wrapped = executor_result_schema(business)["$defs"]["business_result"]["anyOf"][0]
    assert wrapped[keyword] == {
        "schema": {"$ref": "#/$defs/business_result/anyOf/0/target"},
        "boolean": False,
        "fields": ["name"],
    }


@pytest.mark.parametrize(
    "keyword",
    [
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
    ],
)
def test_schema_subschema_positions_relocate_refs(keyword: str) -> None:
    subschema: Any = (
        [{"$ref": "#"}, False]
        if keyword in {"allOf", "anyOf", "oneOf", "prefixItems", "items"}
        else {"$ref": "#"}
    )
    wrapped = executor_result_schema({keyword: subschema})["$defs"]["business_result"]["anyOf"][0]
    expected: Any = {"$ref": "#/$defs/business_result/anyOf/0"}
    if isinstance(subschema, list):
        expected = [expected, False]
    assert wrapped[keyword] == expected


@pytest.mark.parametrize("reference", ["#", "#/$defs/value"])
@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
def test_local_refs_remain_valid_after_wrapping(keyword: str, reference: str) -> None:
    business = {
        "type": "object",
        "$defs": {"value": {"type": "integer"}},
        "properties": {"value": {keyword: reference}},
    }
    validator = Draft202012Validator(executor_result_schema(business))
    valid: Any = {} if reference == "#" else 1
    invalid: Any = "invalid"
    assert validator.is_valid({"result": {"value": valid}})
    assert not validator.is_valid({"result": {"value": invalid}})


@pytest.mark.parametrize(
    "reference", ["#value", "other.json#/$defs/value", "https://example.com/value"]
)
def test_named_and_external_refs_are_preserved(reference: str) -> None:
    wrapped = executor_result_schema({"$ref": reference})["$defs"]["business_result"]["anyOf"][0]
    assert wrapped["$ref"] == reference


@pytest.mark.parametrize("resource_at_root", [True, False])
def test_identified_schema_resources_keep_their_own_reference_scope(resource_at_root: bool) -> None:
    resource = {
        "$id": "https://example.com/resource",
        "type": "object",
        "$defs": {"value": {"type": "integer"}},
        "properties": {"value": {"$ref": "#/$defs/value"}},
    }
    business = (
        resource if resource_at_root else {"type": "object", "properties": {"nested": resource}}
    )
    original = deepcopy(business)
    schema = executor_result_schema(business)
    valid = {"value": 1} if resource_at_root else {"nested": {"value": 1}}
    invalid = {"value": "wrong"} if resource_at_root else {"nested": {"value": "wrong"}}
    assert Draft202012Validator(schema).is_valid({"result": valid})
    assert not Draft202012Validator(schema).is_valid({"result": invalid})
    assert business == original


def test_escaped_json_pointer_tokens_remain_resolvable() -> None:
    schema = executor_result_schema(
        {
            "type": "object",
            "$defs": {"a/b~c": {"type": "integer"}},
            "properties": {"value": {"$ref": "#/$defs/a~1b~0c"}},
        }
    )
    validator = Draft202012Validator(schema)
    assert validator.is_valid({"result": {"value": 1}})
    assert not validator.is_valid({"result": {"value": "wrong"}})


@pytest.mark.parametrize("empty_id_at_root", [True, False])
def test_empty_schema_id_does_not_create_a_new_reference_scope(empty_id_at_root: bool) -> None:
    business: dict[str, Any] = {
        "type": "object",
        "properties": {"value": {"type": "integer"}, "copy": {"$ref": "#/properties/value"}},
        "required": ["value", "copy"],
    }
    target = business if empty_id_at_root else business["properties"]["copy"]
    target["$id"] = ""
    original = deepcopy(business)
    schema = executor_result_schema(business)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert validator.is_valid({"result": {"value": 4, "copy": 4}})
    assert not validator.is_valid({"result": {"value": 4, "copy": "wrong"}})
    assert business == original
