"""Test schema validation helpers in core/utils/helpers.py"""

import logging
from typing import List

import pytest
from pydantic import BaseModel

from apflow.core.utils.helpers import (
    check_input_schema,
    resolve_schema_refs,
    validate_input_schema,
)


class TestResolveSchemaRefs:
    """Regression: resolve_schema_refs had no cycle guard, so a
    self-referential inputs_schema/outputs_schema crashed get_input_schema/
    get_output_schema with RecursionError. (Review CRITICAL #50)
    """

    def test_self_referential_schema_does_not_recurse_infinitely(self):
        class TreeNode(BaseModel):
            name: str
            children: List["TreeNode"] = []

        TreeNode.model_rebuild()

        schema = TreeNode.model_json_schema()

        # Must not raise RecursionError.
        resolved = resolve_schema_refs(schema)

        assert "$defs" not in resolved
        assert resolved["type"] == "object"
        # The nested self-reference inside children.items is a genuine cycle
        # and must remain as an unresolved $ref rather than expand forever.
        children_items = resolved["properties"]["children"]["items"]
        assert "$ref" in children_items

    def test_non_recursive_schema_still_fully_inlined(self):
        """Regression guard: ordinary (non-cyclic) nested refs must still be
        fully resolved, not left dangling."""

        class Address(BaseModel):
            city: str

        class Person(BaseModel):
            name: str
            address: Address

        resolved = resolve_schema_refs(Person.model_json_schema())

        assert "$defs" not in resolved
        assert "$ref" not in resolved["properties"]["address"]
        assert resolved["properties"]["address"]["properties"]["city"]["type"] == "string"


class TestCheckInputSchema:
    """Regression: check_input_schema raised a factually wrong 'Missing
    required parameters: []' message for type-validation failures where every
    required field was actually present. (Review CRITICAL #17/#51)
    """

    class _Schema(BaseModel):
        name: str
        count: int

    def test_missing_field_reports_missing(self):
        with pytest.raises(ValueError, match=r"Missing required parameters: \['count'\]"):
            check_input_schema(self._Schema, {"name": "x"})

    def test_type_mismatch_does_not_falsely_report_missing(self):
        with pytest.raises(ValueError) as exc_info:
            check_input_schema(self._Schema, {"name": "x", "count": "not-a-number"})

        message = str(exc_info.value)
        assert "Missing required parameters: []" not in message
        assert "Invalid parameters" in message

    def test_valid_parameters_do_not_raise(self):
        check_input_schema(self._Schema, {"name": "x", "count": 1})


class TestValidateInputSchema:
    """Regression: validate_input_schema swallowed every exception type into
    bare False (masking programmer bugs) and logged raw parameter values
    (which may contain secrets) on every failure. (Review CRITICAL #52/#53)
    """

    class _Schema(BaseModel):
        name: str
        count: int

    def test_valid_parameters_return_true(self):
        assert validate_input_schema(self._Schema, {"name": "x", "count": 1}) is True

    def test_invalid_parameters_return_false(self):
        assert validate_input_schema(self._Schema, {"name": "x", "count": "nope"}) is False

    def test_validation_failure_does_not_log_raw_parameter_values(self, caplog):
        secret_value = "super-secret-token-value"
        with caplog.at_level(logging.ERROR):
            result = validate_input_schema(self._Schema, {"name": "x", "count": secret_value})

        assert result is False
        assert secret_value not in caplog.text

    def test_non_validation_exception_propagates_as_programmer_bug(self):
        class BrokenSchema:
            def __init__(self, **kwargs: object):
                raise TypeError("simulated bug: schema misconfigured")

        with pytest.raises(TypeError, match="simulated bug"):
            validate_input_schema(BrokenSchema, {"name": "x"})  # type: ignore[arg-type]
