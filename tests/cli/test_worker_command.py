"""Tests for the `apflow worker` command's --log-level handling (apflow.cli.worker)."""

from __future__ import annotations

from click.testing import CliRunner

from apflow.cli import worker


def test_worker_log_level_calls_setup_logging(monkeypatch) -> None:
    """Regression: --log-level only called
    logging.getLogger("apflow").setLevel(...) with no handler ever attached,
    so DEBUG/INFO messages were silently dropped — apflow.logger.setup_logging()
    (which correctly wires logging.basicConfig()) was never invoked, making
    it dead code. (Review CRITICAL #16)
    """
    calls: list[object] = []
    monkeypatch.setattr("apflow.logger.setup_logging", lambda level=None: calls.append(level))

    # setup_logging runs before create_app(); make create_app fail immediately
    # afterward so the rest of the command's heavier distributed-runtime setup
    # never has to be mocked.
    def fake_create_app(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("stop-early")

    monkeypatch.setattr("apflow.app.create_app", fake_create_app)

    CliRunner().invoke(worker, ["--db", "postgresql://x", "--log-level", "DEBUG"])

    assert calls == ["DEBUG"]


def test_worker_without_log_level_does_not_call_setup_logging(monkeypatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr("apflow.logger.setup_logging", lambda level=None: calls.append(level))

    def fake_create_app(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("stop-early")

    monkeypatch.setattr("apflow.app.create_app", fake_create_app)

    CliRunner().invoke(worker, ["--db", "postgresql://x"])

    assert calls == []
