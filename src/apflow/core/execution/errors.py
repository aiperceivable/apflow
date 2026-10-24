"""
Custom exceptions for task execution.

This module defines exceptions that represent different failure modes
during task execution, following best practices from FastAPI and other
production frameworks.

Exception Hierarchy:
    ApflowError (base)
        ├── BusinessError (user/expected errors, no stack trace)
        │   ├── ValidationError (input validation failures)
        │   ├── ConfigurationError (config/environment issues)
        │   ├── NetworkError (network/connection failures)
        │   └── AuthenticationError (auth failures)
        └── SystemError (unexpected errors, with stack trace)
            ├── ExecutorError (executor runtime failures)
            └── StorageError (database/storage failures)

Usage Guidelines:
    - Raise BusinessError subclasses for expected failures (bad input, missing config)
    - Use structured error format: what/why/how_to_fix/context
    - Let system exceptions (TimeoutError, ConnectionError) propagate naturally
    - TaskManager logs BusinessError without exc_info, others with exc_info

Structured Error Format:
    All error classes support optional structured information:
    - what: What went wrong (brief description)
    - why: Why it happened (root cause)
    - how_to_fix: How to resolve it (actionable steps)
    - context: Additional context (dict with relevant details)
"""

from typing import Any, ClassVar

from apflow.core.execution.error_info import (
    ErrorInfo,
    execution_error_info as execution_error_info,
    http_error_info as http_error_info,
    validate_error_info,
)


class ApflowError(RuntimeError):
    """
    Base exception for all AiPartnerUpFlow-specific errors.

    This serves as the root of the exception hierarchy and allows
    catching all framework-specific exceptions if needed.

    Supports structured error information:
    - what: What went wrong
    - why: Why it happened
    - how_to_fix: How to resolve it
    - context: Additional context dict
    """

    def __init__(
        self,
        message: str,
        *,
        what: str | None = None,
        why: str | None = None,
        how_to_fix: str | None = None,
        context: dict[str, Any] | None = None,
        error_info: ErrorInfo | None = None,
        token_usage: dict[str, int] | None = None,
    ) -> None:
        """
        Initialize error with optional structured information.

        Args:
            message: Error message (used if structured info not provided)
            what: Brief description of what went wrong
            why: Root cause explanation
            how_to_fix: Actionable resolution steps
            context: Additional context dictionary
        """
        self.what = what
        self.why = why
        self.how_to_fix = how_to_fix
        self.context = context or {}
        self.error_info = validate_error_info(error_info) if error_info is not None else None
        self.token_usage = token_usage

        # Build formatted message
        if what:
            formatted_msg = f"❌ {what}"
            if why:
                formatted_msg += f"\n\n💡 Reason: {why}"
            if how_to_fix:
                formatted_msg += f"\n\n✅ Solution: {how_to_fix}"
            if context:
                context_str = "\n".join(f"  - {k}: {v}" for k, v in context.items())
                formatted_msg += f"\n\n📝 Context:\n{context_str}"
            super().__init__(formatted_msg)
        else:
            super().__init__(message)


class BusinessError(ApflowError):
    """
    Base exception for expected/user-facing failures.

    Use this exception for:
    - Validation errors (invalid input, missing required fields)
    - Configuration errors (missing API keys, invalid settings)
    - Permission errors (unauthorized access, quota exceeded)
    - Resource errors (file not found, resource unavailable)

    These errors represent expected failure modes that don't require
    stack traces for debugging. The TaskManager will log them without
    exc_info=True to keep logs clean.

    For unexpected system errors (network failures, timeouts, service errors),
    let the original exception propagate so it includes a full stack trace.

    Example:
        >>> if not inputs.get("api_key"):
        >>>     raise ConfigurationError("API key is required")
    """

    permanent_failure: ClassVar[bool] = True


class ValidationError(BusinessError):
    """
    Validation-specific business error.

    Use this for input validation failures, schema mismatches,
    or constraint violations that are caused by user input.

    Example:
        >>> if not inputs.get("model"):
        >>>     raise ValidationError("model is required in inputs")
    """

    pass


class ConfigurationError(BusinessError):
    """
    Configuration-specific business error.

    Use this for missing configuration, invalid settings,
    or environment setup issues.

    Example:
        >>> if not LITELLM_AVAILABLE:
        >>>     raise ConfigurationError("litellm is not installed")
    """

    pass


class NetworkError(BusinessError):
    """
    Network or connection-related business error.

    Use this for network failures, connection timeouts,
    or unreachable services that are expected failure modes.

    Example:
        >>> raise NetworkError(
        >>>     "Failed to connect",
        >>>     what="HTTP request failed",
        >>>     why=f"Cannot connect to {url}: Connection timeout",
        >>>     how_to_fix="1. Check URL\n2. Verify service is running\n3. Check firewall",
        >>>     context={"url": url, "timeout": 30}
        >>> )
    """

    permanent_failure: ClassVar[bool] = False


class AuthenticationError(BusinessError):
    """
    Authentication or authorization error.

    Use this for auth failures, invalid credentials,
    or permission denials.

    Example:
        >>> raise AuthenticationError(
        >>>     "Authentication failed",
        >>>     what="SSH authentication failed",
        >>>     why=f"Cannot authenticate with key {key_path}",
        >>>     how_to_fix="1. Check key permissions (chmod 600)\n2. Add key to ssh-agent",
        >>>     context={"host": host, "key_path": key_path}
        >>> )
    """

    pass


class SystemError(ApflowError):
    """
    Base exception for unexpected system-level errors.

    These are logged with full stack traces since they represent
    unexpected failures that need investigation.

    Note: Use this sparingly - most system errors (TimeoutError,
    ConnectionError, etc.) should be allowed to propagate naturally.
    """

    pass


class ExecutorError(SystemError):
    """
    Executor runtime error.

    Use this when an executor encounters an unexpected internal error
    that is not a connection/timeout/service error.
    """

    pass


class StorageError(SystemError):
    """
    Database or storage operation error.

    Use this for unexpected database/storage failures that are not
    simple connection errors.
    """

    pass
