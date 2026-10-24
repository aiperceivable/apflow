"""Bounded, versioned execution failure metadata shared across transports."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any, Literal, NotRequired, TypedDict, cast


class ErrorInfo(TypedDict):
    """Safe machine-readable metadata; business output stays in result."""

    version: Literal[1]
    code: str
    category: str
    retryable: bool
    delivery_state: str
    origin: str
    phase: str
    observed_at: str
    task_id: NotRequired[str]
    attempt_id: NotRequired[int]
    request_id: NotRequired[str]
    upstream_code: NotRequired[str]
    http_status: NotRequired[int]
    retry_after_seconds: NotRequired[float]


ERROR_INFO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "version",
        "code",
        "category",
        "retryable",
        "delivery_state",
        "origin",
        "phase",
        "observed_at",
    ],
    "properties": {
        "version": {"const": 1, "type": "integer"},
        "code": {"type": "string", "minLength": 1, "maxLength": 255},
        "category": {
            "enum": ["permanent", "transient", "unknown", "cancelled", "dependency", "business"]
        },
        "retryable": {"type": "boolean"},
        "delivery_state": {"enum": ["not_sent", "responded", "unknown"]},
        "origin": {"type": "string", "minLength": 1, "maxLength": 255},
        "phase": {"type": "string", "minLength": 1, "maxLength": 255},
        "observed_at": {"type": "string", "minLength": 1, "maxLength": 255},
        "task_id": {"type": "string", "minLength": 1, "maxLength": 255},
        "attempt_id": {"type": "integer", "minimum": 0},
        "request_id": {"type": "string", "minLength": 1, "maxLength": 255},
        "upstream_code": {"type": "string", "minLength": 1, "maxLength": 255},
        "http_status": {"type": "integer", "minimum": 100, "maximum": 599},
        "retry_after_seconds": {"type": "number", "minimum": 0, "maximum": 86400},
    },
    "additionalProperties": False,
}


def validate_error_info(value: Any) -> ErrorInfo:
    """Validate the same small schema used by registry output validation."""
    properties = ERROR_INFO_SCHEMA["properties"]
    if not isinstance(value, dict) or set(value) - set(properties):
        raise ValueError("error_info must match execution error schema version 1")
    if not set(ERROR_INFO_SCHEMA["required"]) <= set(value):
        raise ValueError("error_info is missing required version 1 fields")
    for key, field in value.items():
        spec = properties[key]
        kind = spec.get("type")
        valid = True
        if kind == "string":
            valid = isinstance(field, str) and 1 <= len(field) <= 255
        elif kind in {"integer", "number"}:
            numeric = (
                isinstance(field, int) if kind == "integer" else isinstance(field, (int, float))
            )
            valid = (
                numeric
                and not isinstance(field, bool)
                and (not isinstance(field, float) or isfinite(field))
            )
            valid = valid and spec.get("minimum", float("-inf")) <= field <= spec.get(
                "maximum", float("inf")
            )
        elif kind == "boolean":
            valid = isinstance(field, bool)
        if "enum" in spec:
            valid = valid and field in spec["enum"]
        if "const" in spec:
            valid = valid and field == spec["const"]
        if not valid:
            raise ValueError("error_info must match execution error schema version 1")
    return cast(ErrorInfo, dict(value))


def execution_error_info(
    error: BaseException,
    *,
    task_id: str | None = None,
    attempt_id: int | None = None,
    phase: str = "execution",
) -> ErrorInfo:
    """Preserve typed failures, without serializing stack traces or raw context."""
    from apcore.errors import ModuleError

    existing = getattr(error, "error_info", None)
    if existing:
        info = validate_error_info(existing)
    else:
        permanent = isinstance(error, (ValueError, TypeError, PermissionError)) or getattr(
            error, "permanent_failure", False
        )
        module_error = isinstance(error, ModuleError)
        if module_error:
            permanent = error.user_fixable or error.code in {
                "ACL_DENIED",
                "INVALID_INPUT",
                "SCHEMA_VALIDATION_ERROR",
                "MODULE_NOT_FOUND",
                "APPROVAL_REQUIRED",
                "APPROVAL_DENIED",
                "AUTHENTICATION_ERROR",
                "CONFIGURATION_ERROR",
                "MODULE_DISABLED",
                "TASK_IMMUTABLE",
                "IDEMPOTENCY_CONFLICT",
            }
        info = {
            "version": 1,
            "code": error.code if module_error else type(error).__name__,
            "category": "permanent" if permanent else "unknown",
            "retryable": False,
            "delivery_state": "not_sent" if permanent else "unknown",
            "origin": "apcore" if module_error else "apflow",
            "phase": phase,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    if task_id is not None:
        info["task_id"] = task_id
    if attempt_id is not None:
        info["attempt_id"] = attempt_id
    return validate_error_info(info)


def parse_retry_after(value: str | None) -> float | None:
    """Parse HTTP delta seconds or HTTP date, ignoring malformed values."""
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            delay = (target - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, delay) if isfinite(delay) else None


def http_error_info(
    status_code: int,
    method: str,
    *,
    retry_safe: bool = False,
    retry_after: str | None = None,
    upstream_code: str | None = None,
    request_id: str | None = None,
    delivery_state: str = "responded",
) -> ErrorInfo:
    """Only replay read-only requests or operations declared safe by trusted code."""
    transient = status_code in {408, 425, 429, 500, 502, 503, 504}
    delay = parse_retry_after(retry_after)
    info: ErrorInfo = {
        "version": 1,
        "code": f"HTTP_{status_code}",
        "category": "transient" if transient else "permanent",
        "retryable": transient and (retry_safe or method.upper() in {"GET", "HEAD", "OPTIONS"}),
        "delivery_state": delivery_state,
        "origin": "http",
        "phase": "request",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "http_status": status_code,
    }
    if delay is not None:
        info["retry_after_seconds"] = min(delay, 86400.0)
        if delay > 86400:
            info["retryable"] = False
    if upstream_code:
        info["upstream_code"] = upstream_code[:255]
    if request_id:
        info["request_id"] = request_id[:255]
    return validate_error_info(info)
