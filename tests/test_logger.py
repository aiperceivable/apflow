"""Tests for apflow.logger.setup_logging level normalization."""

from __future__ import annotations

import logging

import pytest

from apflow.logger import setup_logging


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "critical"])
def test_setup_logging_accepts_lowercase_levels(level: str) -> None:
    """Regression (Review CRITICAL): a lowercase --log-level crashed with
    TypeError because setup_logging only uppercased when level was None;
    getattr(logging, "debug") resolves to the *function*, which
    basicConfig(level=...) rejects. setup_logging must normalize any source.
    """
    # Must not raise TypeError("Level not an integer or a valid string ...").
    setup_logging(level)

    expected = getattr(logging, level.upper())
    assert logging.getLogger("apflow").level == expected


def test_setup_logging_uppercase_still_works() -> None:
    setup_logging("WARNING")
    assert logging.getLogger("apflow").level == logging.WARNING


def test_setup_logging_none_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APFLOW_LOG_LEVEL", "error")
    setup_logging(None)
    assert logging.getLogger("apflow").level == logging.ERROR
