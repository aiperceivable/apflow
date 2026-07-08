"""Regression (CRITICAL): bridge handlers must raise typed apcore Moduleerrors
so a missing task maps to HTTP 404 and bad input to 422 — not the 500 that raw
KeyError/ValueError produced (apcore wraps them into MODULE_EXECUTE_ERROR)."""

from __future__ import annotations

from apcore.errors import ModuleError

from apflow.api.rest import _status_for_error
from apflow.bridge.errors import invalid_input_error, not_found_error


def test_not_found_error_maps_to_http_404() -> None:
    err = not_found_error("Task 'X' not found")
    assert isinstance(err, ModuleError)
    assert err.code == "MODULE_NOT_FOUND"
    assert _status_for_error(err) == 404


def test_invalid_input_error_maps_to_http_422() -> None:
    err = invalid_input_error("task_id must be non-empty")
    assert isinstance(err, ModuleError)
    assert err.code == "INVALID_INPUT"
    assert _status_for_error(err) == 422
