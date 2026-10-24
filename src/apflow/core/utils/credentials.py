"""Service-owned connection profiles and safe public task serialization."""

from __future__ import annotations

import copy
import logging
import traceback
import os
import posixpath
import re
import uuid
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from apflow.core.config_manager import get_config_manager
from apflow.core.execution.errors import ValidationError

_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "password",
        "smtppassword",
        "apikey",
        "authtoken",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "token",
        "cookie",
        "setcookie",
    }
)
_AUTH_PATTERN = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[-_\s]", "", key.lower())
    return (
        key.lower().startswith("_secret_")
        or normalized in _SENSITIVE_KEYS
        or normalized.removeprefix("x") in _SENSITIVE_KEYS
    )


def redact_public_data(value: Any, schema: dict[str, Any] | None = None) -> Any:
    """Return a detached public value with known credential fields removed."""
    if isinstance(value, dict):
        from apcore import redact_sensitive

        value = redact_sensitive(value, schema or {}, sensitive_keys=[])
    entries = get_config_manager().get("security.credentials", [])
    secrets = [
        os.environ.get(entry.get("env_var", ""), "") for entry in entries if isinstance(entry, dict)
    ]
    return _redact_value(value, [secret for secret in secrets if secret])


def _redact_value(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        redacted = {
            key: (
                "***REDACTED***"
                if _sensitive_key(str(key)) and item is not None
                else _redact_value(item, secrets)
            )
            for key, item in value.items()
        }
        if str(value.get("type", "")).lower() == "apikey" and "value" in value:
            redacted["value"] = "***REDACTED***"
        return redacted
    if isinstance(value, (tuple, list)):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "***REDACTED***")
        value = _AUTH_PATTERN.sub(r"\1 ***REDACTED***", value)
        value = re.sub(r"(https?://)[^/@\s]+@", r"\1***REDACTED***@", value)
        return re.sub(
            r"([?&](?:api[_-]?key|token|access_token|password)=)[^&#\s]*",
            r"\1***REDACTED***",
            value,
            flags=re.IGNORECASE,
        )
    return copy.deepcopy(value)


def reject_inline_credentials(value: Any) -> None:
    """Reject known inline credentials before persisting service task inputs."""
    if isinstance(value, dict):
        if str(value.get("type", "")).lower() == "apikey" and value.get("value"):
            raise ValidationError("Inline credentials are not allowed; use credential_ref")
        for key, item in value.items():
            if _sensitive_key(str(key)) and item not in (None, "", "***REDACTED***"):
                raise ValidationError("Inline credentials are not allowed; use credential_ref")
            reject_inline_credentials(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_inline_credentials(item)
    elif isinstance(value, str) and value.startswith(("http://", "https://")):
        parsed = urlsplit(value)
        if parsed.username or any(_sensitive_key(key) for key, _ in parse_qsl(parsed.query)):
            raise ValidationError("URL credentials are not allowed; use credential_ref")


def _configured_entry(key: str, identifier: str) -> dict[str, Any]:
    entries = get_config_manager().get(key, [])
    if not isinstance(entries, list):
        raise ValidationError(f"{key} must be a list of configured profiles")
    matches = [
        entry for entry in entries if isinstance(entry, dict) and entry.get("id") == identifier
    ]
    if len(matches) != 1:
        raise ValidationError("Unknown or duplicate connection profile or credential reference")
    return matches[0]


def get_connection_profile(identifier: str) -> dict[str, Any]:
    """Read a service-owned profile; task input never defines policy."""
    return _configured_entry("network.profiles", identifier)


def _origin(url: str) -> tuple[str, str | None, int]:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Invalid URL")
        return (
            parsed.scheme,
            parsed.hostname.lower(),
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except ValueError as error:
        raise ValidationError(
            "Connection URL must be an HTTP(S) URL without credentials"
        ) from error


def validate_profile_target(url: str, method: str, profile: dict[str, Any]) -> None:
    """Validate exact origin and normalized path before sending profile credentials."""
    if _origin(url) != _origin(str(profile.get("base_url", ""))):
        raise ValidationError("URL is outside the connection profile origin")
    if method.upper() not in profile.get("allowed_methods", ["GET"]):
        raise ValidationError("HTTP method is outside the connection profile policy")
    path = unquote(urlsplit(url).path or "/")
    if "\\" in path or "%" in path or any(part in (".", "..") for part in path.split("/")):
        raise ValidationError("URL path contains ambiguous traversal or encoding")
    normalized = posixpath.normpath(path)
    prefixes = profile.get("allowed_path_prefixes", [urlsplit(profile["base_url"]).path or "/"])
    if not any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix.rstrip("/") + "/")
        for prefix in prefixes
    ):
        raise ValidationError("URL is outside the connection profile path policy")


def _authorize_profile(profile: dict[str, Any], context: Any) -> None:
    subjects = profile.get("allowed_subjects")
    if subjects is None:
        return
    from apflow.core.execution.context import execution_context

    effective = context if context is not None else execution_context.get()
    identity = getattr(effective, "identity", None)
    if identity is None or identity.id not in subjects:
        raise ValidationError("Authenticated subject cannot use this connection profile")


def _correlate_headers(inputs: dict[str, Any], context: Any) -> None:
    from apflow.core.execution.context import execution_context

    effective = context if context is not None else execution_context.get()
    trace_id = getattr(effective, "trace_id", None)
    headers = inputs.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValidationError("HTTP headers must be an object")
    request_keys = [key for key in headers if key.lower() == "x-request-id"]
    if len(request_keys) > 1:
        raise ValidationError("Duplicate request correlation header")
    if request_keys and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", str(headers[request_keys[0]])):
        raise ValidationError("Request correlation header is invalid")
    if isinstance(trace_id, str) and re.fullmatch(r"[a-f0-9]{32}", trace_id):
        if not request_keys:
            headers["X-Request-ID"] = trace_id
        for key in list(headers):
            if key.lower() == "traceparent":
                del headers[key]
        headers["traceparent"] = f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01"
    if headers:
        inputs["headers"] = headers


def _inject_credential(inputs: dict[str, Any], profile: dict[str, Any]) -> None:
    reference = inputs.get("credential_ref") or profile.get("credential_ref")
    if not reference:
        return
    if reference != profile.get("credential_ref"):
        raise ValidationError("credential_ref is outside the connection profile policy")
    credential = _configured_entry("security.credentials", reference)
    if profile["id"] not in credential.get("allowed_profiles", []):
        raise ValidationError("credential_ref is not authorized for this connection profile")
    value = os.environ.get(str(credential.get("env_var", "")))
    if not value:
        raise ValidationError("Configured credential is unavailable")
    header = str(credential.get("header", "Authorization"))
    headers = inputs.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValidationError("HTTP headers must be an object")
    inputs["headers"] = headers
    if (
        any(key.lower() == header.lower() for key in headers)
        or inputs.get("auth")
        or inputs.get("auth_token")
    ):
        raise ValidationError("Task input cannot override a profile credential")
    headers[header] = str(credential.get("prefix", "Bearer ")) + value


def resolve_outbound_inputs(
    inputs: dict[str, Any], url_field: str = "url", context: Any = None
) -> dict[str, Any]:
    """Resolve credentials only into an execution copy after target authorization."""
    resolved = copy.deepcopy(inputs)
    _correlate_headers(resolved, context)
    identifier = resolved.get("connection_profile")
    if not identifier:
        if resolved.get("credential_ref"):
            raise ValidationError("credential_ref requires a connection_profile")
        return resolved
    profile = get_connection_profile(identifier)
    if resolved.get("verify") is False:
        raise ValidationError("Connection profile requires TLS certificate verification")
    _authorize_profile(profile, context)
    url = resolved.get(url_field) or profile.get("base_url")
    if not isinstance(url, str):
        raise ValidationError("Connection profile requires a target URL")
    method = "POST" if url_field == "base_url" else str(resolved.get("method", "GET"))
    if url_field == "base_url":
        if _origin(url) != _origin(str(profile.get("base_url", ""))):
            raise ValidationError("URL is outside the connection profile origin")
    else:
        validate_profile_target(url, method, profile)
    reject_inline_credentials(resolved)
    resolved[url_field] = url
    _inject_credential(resolved, profile)
    return resolved


def log_public_exception(
    logger: logging.Logger, message: str, error: BaseException, *, include_traceback: bool = True
) -> None:
    """Redact a complete exception chain before handing diagnostics to log handlers."""
    diagnostic = "".join(traceback.format_exception(error)) if include_traceback else str(error)
    logger.error("%s: %s", message, redact_public_data(diagnostic))
