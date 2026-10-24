"""Project native validation schemas into contracts for redacted MCP results."""

from copy import deepcopy
import json
from typing import Any

from referencing import Registry, Resource, Specification
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012, specification_with

_SHAPE_KEYWORDS = (
    "type",
    "title",
    "description",
    "required",
    "minProperties",
    "maxProperties",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
)


def public_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Keep public structure; native execution still validates all value constraints.

    Redaction can replace any field with a marker or change arbitrary strings.
    Value predicates, conditional branches, and item uniqueness therefore belong
    to native validation. Canonical definitions preserve local reference targets
    even when those predicates or exclusive unions disappear from the public view.
    """
    # Shared Python fragments occupy distinct locations in the JSON wire schema.
    schema = json.loads(json.dumps(schema, allow_nan=False))
    dialect = schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
    specification = specification_with(dialect, default=DRAFT202012)
    resource = Resource.from_contents(schema, default_specification=specification)
    projection = _OutputSchemaProjection(specification, "/draft-07/" in dialect)
    root = projection.project(schema, Registry().resolver_with_root(resource))
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "allOf": [root],
        "$defs": projection.definitions,
    }


class _OutputSchemaProjection:
    """Build a finite reference graph without changing native schemas or scopes."""

    def __init__(self, specification: Specification[Any], legacy_references: bool) -> None:
        self._specification = specification
        self._legacy_references = legacy_references
        self._names: dict[int, str] = {}
        self.definitions: dict[str, Any] = {}

    def project(self, schema: dict[str, Any] | bool, resolver: Any) -> dict[str, Any] | bool:
        if isinstance(schema, bool):
            return schema
        identity = id(schema)
        if identity not in self._names:
            name = "value" + str(len(self._names))
            self._names[identity] = name
            self.definitions[name] = {}
            self.definitions[name] = {
                "anyOf": [{"const": "***REDACTED***"}, self._shape(schema, resolver)]
            }
        return {"$ref": "#/$defs/" + self._names[identity]}

    def _child(self, schema: dict[str, Any] | bool, resolver: Any) -> dict[str, Any] | bool:
        resource = Resource.from_contents(schema, default_specification=self._specification)
        return self.project(schema, resolver.in_subresource(resource))

    def _shape(self, schema: dict[str, Any], resolver: Any) -> dict[str, Any] | bool:
        constraints: list[dict[str, Any] | bool] = []
        if "$ref" in schema:
            try:
                target = resolver.lookup(schema["$ref"])
            except Unresolvable:
                raise ValueError("MCP output schema contains an unresolved reference") from None
            reference = self.project(target.contents, target.resolver)
            if self._legacy_references:
                return reference
            constraints.append(reference)
        shape = {name: deepcopy(schema[name]) for name in _SHAPE_KEYWORDS if name in schema}
        for keyword in ("properties", "patternProperties"):
            if keyword in schema:
                shape[keyword] = {
                    name: self._child(child, resolver) for name, child in schema[keyword].items()
                }
        for keyword in ("items", "prefixItems", "additionalProperties"):
            if keyword == "prefixItems" and self._specification is not DRAFT202012:
                continue
            if keyword in schema:
                child = schema[keyword]
                if isinstance(child, list):
                    shape["prefixItems"] = [self._child(item, resolver) for item in child]
                    if keyword == "items":
                        shape["items"] = self._child(schema.get("additionalItems", True), resolver)
                else:
                    shape[keyword] = self._child(child, resolver)
        for keyword in ("allOf", "anyOf", "oneOf"):
            if keyword in schema:
                branches = [self._child(child, resolver) for child in schema[keyword]]
                constraints.append({"allOf" if keyword == "allOf" else "anyOf": branches})
        if constraints:
            shape["allOf"] = constraints
        return shape
