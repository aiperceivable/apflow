"""Tests for the `apflow scheduler` command (apflow.cli.scheduler).

The command wraps the internal scheduler's run loop. These cover that CLI
options map onto SchedulerConfig and that --db binds the global session pool
up front (the scheduler's direct-DB path reads the pool, not the --db flag).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from click.testing import CliRunner

from apflow.cli import scheduler


def test_scheduler_maps_options_to_config(monkeypatch) -> None:
    """CLI options are forwarded to SchedulerConfig and run_scheduler is invoked."""
    captured: dict = {}

    async def fake_run_scheduler(config: object, verbose: bool = False) -> None:
        captured["config"] = config
        captured["verbose"] = verbose

    monkeypatch.setattr("apflow.scheduler.internal.run_scheduler", fake_run_scheduler)

    result = CliRunner().invoke(
        scheduler,
        ["--poll-interval", "5", "--max-concurrent", "3", "--user-id", "u1", "--verbose"],
    )

    assert result.exit_code == 0, result.output
    config = captured["config"]
    assert config.poll_interval == 5
    assert config.max_concurrent_tasks == 3
    assert config.user_id == "u1"
    assert captured["verbose"] is True


def test_scheduler_db_binds_pool_before_running(monkeypatch) -> None:
    """--db initializes the global session pool so the direct-DB path uses it."""

    async def fake_run_scheduler(config: object, verbose: bool = False) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr("apflow.scheduler.internal.run_scheduler", fake_run_scheduler)

    pool = MagicMock()
    monkeypatch.setattr("apflow.core.storage.factory.get_session_pool_manager", lambda: pool)

    result = CliRunner().invoke(scheduler, ["--db", "sqlite:///:memory:"])

    assert result.exit_code == 0, result.output
    pool.initialize.assert_called_once_with(connection_string="sqlite:///:memory:")
