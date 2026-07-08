"""
Helper utilities - demonstrates correct logging usage in utils modules
"""

import logging
from datetime import datetime
from typing import Any, Dict, FrozenSet, List, Type
from pydantic import BaseModel, HttpUrl, ValidationError
from urllib.parse import urlparse, urlunparse, ParseResult

# Use standard library logging directly
logger = logging.getLogger(__name__)


def parse_iso_datetime(value: str) -> datetime:
    """Parse ISO 8601 datetime string, handling 'Z' suffix for UTC.

    Python 3.10 and below do not support 'Z' in fromisoformat().
    This function normalises the suffix before parsing.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def merge_dicts(dict1: Dict, dict2: Dict) -> Dict:
    """Merge two dictionaries - pure utility function"""
    return {**dict1, **dict2}


def pick_dict(dict: Dict, keys: List[str]) -> Dict:
    """Pick keys from dictionary - pure utility function"""
    return {k: v for k, v in dict.items() if k in keys}


def exclude_dict_keys(dict: Dict, keys: List[str]) -> Dict:
    """Exclude keys from dictionary - pure utility function"""
    return {k: v for k, v in dict.items() if k not in keys}


def chunk_list(lst: List, chunk_size: int) -> List[List]:
    """Split list into chunks - pure utility function"""
    return [lst[i : i + chunk_size] for i in range(0, len(lst), chunk_size)]


def safe_get_nested(data: Dict, keys: List[str], default: Any = None) -> Any:
    """Safely get nested dictionary value - pure utility function"""
    current = data
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def resolve_schema_refs(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve $ref references in JSON Schema by inlining $defs.

    Pydantic's model_json_schema() produces $defs/$ref for nested models.
    Consumers (e.g. task_manager) expect flat inline JSON Schema.
    This helper inlines all $ref references and removes the $defs block.

    Args:
        schema: JSON Schema dict potentially containing $defs and $ref

    Returns:
        JSON Schema dict with all $ref resolved inline
    """
    if "$defs" not in schema:
        return schema

    defs = schema.get("$defs", {})

    def _resolve(node: Any, expanding: FrozenSet[str]) -> Any:
        if isinstance(node, dict):
            # Handle $ref
            if "$ref" in node:
                ref_path = node["$ref"]  # e.g. "#/$defs/AuthConfig"
                ref_name = ref_path.rsplit("/", 1)[-1]
                if ref_name in expanding:
                    # Self-referential cycle (e.g. a recursive tree-node
                    # model): stop inlining here instead of recursing forever
                    # and leave this occurrence as an unresolved $ref.
                    return node
                if ref_name in defs:
                    resolved = _resolve(dict(defs[ref_name]), expanding | {ref_name})
                    # Merge any sibling keys (e.g. description alongside $ref)
                    extra = {k: v for k, v in node.items() if k != "$ref"}
                    if extra:
                        resolved.update(extra)
                    return resolved
                return node

            # Handle anyOf/oneOf/allOf containing $ref
            return {k: _resolve(v, expanding) for k, v in node.items()}

        if isinstance(node, list):
            return [_resolve(item, expanding) for item in node]

        return node

    resolved = _resolve(schema, frozenset())

    # Remove $defs from top-level
    resolved.pop("$defs", None)

    return resolved


def get_input_schema(input_schema: Type[BaseModel] | Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """get input parameters with their metadata (required, type, description, default)"""
    parameters = {}
    if not input_schema:
        return parameters

    if isinstance(input_schema, dict):
        # Handle JSON schema
        return _get_json_schema_info(input_schema)
    else:
        # Handle Pydantic BaseModel
        for field_name, field_info in input_schema.model_fields.items():
            parameters[field_name] = {
                "required": field_info.is_required(),
                "type": str(field_info.annotation),
                "description": field_info.description or "",
                "default": field_info.get_default() if not field_info.is_required() else None,
            }

    return parameters


def _get_json_schema_info(schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Extract parameter information from JSON schema"""
    parameters = {}

    try:
        properties = schema.get("properties", {})
        required_fields = schema.get("required", [])

        for field_name, field_schema in properties.items():
            is_required = field_name in required_fields

            # Extract type information
            field_type = field_schema.get("type", "unknown")

            # Extract description
            description = field_schema.get("description", "")

            # Extract default value (JSON schema doesn't have built-in defaults, but we can check for examples)
            default_value = field_schema.get("default", None)

            # Handle nested objects
            if field_type == "object" and "properties" in field_schema:
                nested_properties = _get_json_schema_info(field_schema)
                # For nested objects, we'll store the nested schema info
                parameters[field_name] = {
                    "required": is_required,
                    "type": "object",
                    "description": description,
                    "default": default_value,
                    "nested_schema": nested_properties,
                }
            else:
                parameters[field_name] = {
                    "required": is_required,
                    "type": field_type,
                    "description": description,
                    "default": default_value,
                }

        return parameters
    except Exception as e:
        logger.error(f"Error extracting JSON schema info: {str(e)}")
        return parameters


def check_input_schema(
    input_schema: Type[BaseModel] | Dict[str, Any] | None, parameters: Dict[str, Any]
) -> None:
    if not input_schema:
        return

    # 1. validate parameters
    if not validate_input_schema(input_schema, parameters):
        # Get parameter info for better error message
        param_info = get_input_schema(input_schema)
        required_params = [name for name, info in param_info.items() if info["required"]]
        missing_params = [param for param in required_params if param not in parameters]

        # Only report "missing" when fields are actually absent — validation
        # can also fail with every required field present (e.g. a type
        # mismatch), in which case missing_params is empty and claiming
        # fields are missing would be factually wrong.
        if missing_params:
            raise ValueError(
                f"Missing required parameters: {missing_params}. Available parameters: {param_info}"
            )
        raise ValueError(
            f"Invalid parameters for schema. Provided keys: {sorted(parameters.keys())}. "
            f"Expected parameters: {param_info}"
        )


def validate_input_schema(
    input_schema: Type[BaseModel] | Dict[str, Any], parameters: Dict[str, Any]
) -> bool:
    """validate parameters using Pydantic schema or JSON schema with field validators"""
    if isinstance(input_schema, dict):
        # Handle JSON schema validation
        return validate_json_schema(input_schema, parameters)

    # Use Pydantic's built-in validation with field validators. Only
    # ValidationError is treated as "invalid user input" — any other
    # exception means input_schema itself is broken (e.g. not a callable
    # BaseModel), which is a programmer bug and must propagate rather than
    # being reported as a validation failure.
    try:
        input_schema(**parameters)
        return True
    except ValidationError as e:
        # Log which fields failed and why, but not the raw parameter values —
        # they may contain secrets (tokens, api keys). str(e) is avoided too:
        # pydantic's own formatting embeds the rejected value per error, so
        # include_input=False is required to keep it out of the log.
        logger.error(
            f"Parameter validation failed for keys {sorted(parameters.keys())}: "
            f"{e.errors(include_url=False, include_input=False)}"
        )
        return False


def validate_json_schema(schema: Dict[str, Any], parameters: Dict[str, Any]) -> bool:
    """Validate parameters against JSON schema"""
    try:
        # Check required fields
        required_fields = schema.get("required", [])
        for field in required_fields:
            if field not in parameters:
                logger.error(f"'{field}' is a required field, but it is missing")
                return False

        # Validate properties
        properties = schema.get("properties", {})
        for field_name, field_value in parameters.items():
            if field_name not in properties:
                logger.warning(f"Unknown field '{field_name}' in parameters")
                continue

            field_schema = properties[field_name]
            if not _validate_field_value(field_value, field_schema):
                logger.error(f"Field '{field_name}' validation failed")
                return False

        return True
    except Exception as e:
        logger.error(f"JSON schema validation error: {str(e)}")
        return False


def _validate_field_value(value: Any, field_schema: Dict[str, Any]) -> bool:
    """Validate a single field value against its schema"""
    try:
        expected_type = field_schema.get("type")

        if expected_type == "string":
            return isinstance(value, str)
        elif expected_type == "integer":
            return isinstance(value, int)
        elif expected_type == "number":
            return isinstance(value, (int, float))
        elif expected_type == "boolean":
            return isinstance(value, bool)
        elif expected_type == "array":
            return isinstance(value, list)
        elif expected_type == "object":
            if not isinstance(value, dict):
                return False
            # Recursively validate nested object
            nested_properties = field_schema.get("properties", {})
            nested_required = field_schema.get("required", [])

            for req_field in nested_required:
                if req_field not in value:
                    return False

            for field_name, field_value in value.items():
                if field_name in nested_properties:
                    if not _validate_field_value(field_value, nested_properties[field_name]):
                        return False

            return True

        return True  # Unknown type, assume valid
    except Exception as e:
        logger.error(f"Field validation error: {str(e)}")
        return False


def get_url_with_host_and_port(host: str, port: int | str) -> str:
    """
    return f"http://{host}:{port}"
    """
    if host == "0.0.0.0" or host == "::":
        host = "localhost"
    return f"http://{host}:{port}"


def replace_non_default_ports(url: str, new_port: int | str) -> str:
    """
    if the port in the URL is not 80 or 443, replace it with the new port and return the new URL
    if the port is 80 or 443, or the port is not specified (using default), return the original URL

    parameters:
        url: original URL string
        new_port: new port number (integer or string)

    return:
        new URL string
    """
    parsed = urlparse(url)

    # check if the port is explicitly specified and not 80 or 443
    if parsed.port is not None and parsed.port not in (80, 443):
        # build new netloc (hostname:new port)
        host = parsed.hostname
        new_netloc = f"{host}:{new_port}"

        # replace netloc and rebuild URL
        new_parsed = parsed._replace(netloc=new_netloc)
        return urlunparse(new_parsed)

    # no need to replace, return the original URL
    return url


def get_base_url(url: str | HttpUrl) -> str:
    """Get the base URL from the given URL"""
    parsed = urlparse(str(url))
    return f"{parsed.scheme}://{parsed.netloc}"


def get_netloc(url: str | HttpUrl) -> str:
    """Get the netloc from the given URL"""
    parsed = urlparse(str(url))
    return parsed.netloc


def get_hostname(url: str | HttpUrl) -> str | None:
    """Get the hostname from the given URL"""
    parsed = urlparse(str(url))
    return parsed.hostname


def validate_url(url: str | HttpUrl, url_name: str = "URL") -> ParseResult:
    if not url:
        raise ValueError(f"{url_name} cannot be empty")

    parsed: ParseResult

    try:
        parsed = urlparse(str(url))
    except Exception:
        raise ValueError(f"{url_name} must be a valid URL")

    # Check if URL has required components
    if not parsed.scheme:
        raise ValueError(f"{url_name} must include a scheme (http:// or https://)")

    if not parsed.netloc:
        raise ValueError(f"{url_name} must include a valid domain")

    # Only allow http and https schemes
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{url_name} must use http:// or https:// scheme")

    # Check if domain has at least one dot (basic domain validation)
    if "." not in parsed.netloc:
        raise ValueError(f"{url_name} must include a valid domain name")

    return parsed


def normalize_input_schema(input_schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert simplified input_schema format to standard JSON Schema format

    Args:
        input_schema: Either simplified format or standard JSON Schema format

    Returns:
        Standard JSON Schema format

    Examples:
        # Simplified format (input)
        {
            "url": {"type": "string", "required": True, "default": "https://example.com"},
            "config": {
                "type": "object",
                "required": True,
                "properties": {
                    "timeout": {"type": "integer", "required": False, "default": 30}
                }
            }
        }

        # Standard JSON Schema format (output)
        {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The url parameter",
                    "default": "https://example.com"
                },
                "config": {
                    "type": "object",
                    "description": "The config parameter",
                    "properties": {
                        "timeout": {
                            "type": "integer",
                            "description": "The timeout parameter",
                            "default": 30
                        }
                    },
                    "required": []
                }
            },
            "required": ["url", "config"]
        }
    """
    if not input_schema:
        return {"type": "object", "properties": {}, "required": []}

    # Check if already in standard format
    if "type" in input_schema and input_schema["type"] == "object" and "properties" in input_schema:
        return input_schema

    properties = {}
    required_fields = []

    for key, value in input_schema.items():
        if not isinstance(value, dict):
            continue

        # Handle nested object properties
        if value.get("type") == "object" and "properties" in value:
            nested_properties = {}
            nested_required = []

            for nested_key, nested_value in value["properties"].items():
                if not isinstance(nested_value, dict):
                    continue

                nested_properties[nested_key] = {
                    "type": nested_value.get("type", "string"),
                    "description": nested_value.get("description", f"The {nested_key} parameter"),
                }

                if "default" in nested_value:
                    nested_properties[nested_key]["default"] = nested_value["default"]

                if nested_value.get("required", False):
                    nested_required.append(nested_key)

            properties[key] = {
                "type": "object",
                "description": value.get("description", f"The {key} parameter"),
                "properties": nested_properties,
                "required": nested_required,
            }
        else:
            # Handle simple properties
            properties[key] = {
                "type": value.get("type", "string"),
                "description": value.get("description", f"The {key} parameter"),
            }

            if "default" in value:
                properties[key]["default"] = value["default"]

        # Check if field is required
        if value.get("required", False):
            required_fields.append(key)

    return {"type": "object", "properties": properties, "required": required_fields}
