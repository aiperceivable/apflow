"""Typed error helpers for apflow bridge module handlers.

Handlers must raise apcore ModuleError subclasses (not raw Python exceptions):
apcore wraps any non-ModuleError into a MODULE_EXECUTE_ERROR, which the REST/MCP/A2A
adapters map to HTTP 500. Using these helpers gives the correct status (404 for a
missing task, 422 for bad client input) instead of misclassifying client errors as
server errors.
"""

from __future__ import annotations

from apcore.errors import ModuleError


def not_found_error(message: str) -> ModuleError:
    """A ModuleError for a missing resource → MODULE_NOT_FOUND → HTTP 404."""
    return ModuleError(code="MODULE_NOT_FOUND", message=message, user_fixable=True)


def invalid_input_error(message: str) -> ModuleError:
    """A ModuleError for bad client input → INVALID_INPUT → HTTP 422."""
    return ModuleError(code="INVALID_INPUT", message=message, user_fixable=True)
