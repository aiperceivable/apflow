"""Executable checks for supported CLI startup and local import tooling."""

import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest


@pytest.fixture(scope="module")
def cli_import_report() -> dict[str, Any]:
    command = """
import json
import sys
import time
started = time.perf_counter()
import apflow.cli
elapsed = time.perf_counter() - started
heavy = [name for name in ('litellm', 'crewai', 'torch', 'transformers') if name in sys.modules]
print(json.dumps({'elapsed': elapsed, 'heavy': heavy}))
"""
    result = subprocess.run(
        [sys.executable, "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_cli_import_finishes_within_startup_budget(cli_import_report: dict[str, Any]) -> None:
    assert cli_import_report["elapsed"] < 1.5


def test_cli_import_keeps_optional_frameworks_unloaded(cli_import_report: dict[str, Any]) -> None:
    assert cli_import_report["heavy"] == []


def test_make_import_checks_use_available_tools() -> None:
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "make",
            "check-imports",
            f"PYTHON={sys.executable}",
            f"APDEV={Path(sys.executable).parent / 'apdev'}",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
