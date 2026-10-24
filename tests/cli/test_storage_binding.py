"""CLI metadata must not initialize storage before options are parsed."""

from typing import Any
from apflow.app import ApflowApp
from pathlib import Path
import pytest

from unittest.mock import patch

from click.testing import CliRunner

from apflow.cli import _build_cli


def test_cli_help_and_version_do_not_create_app() -> None:
    with patch("apflow.app.create_app", side_effect=AssertionError("unexpected storage")):
        command = _build_cli()
        for arguments in (["--help"], ["--version"], ["rest", "--help"]):
            result = CliRunner().invoke(command, arguments)
            assert result.exit_code == 0, result.output


def test_app_pool_uses_explicit_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apflow.app import create_app

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'other.db'}")
    app = create_app(connection_string=f"sqlite:///{tmp_path / 'chosen.db'}")
    try:
        with app.session_factory() as session:
            assert session.get_bind() is app.session.get_bind()
        assert not (tmp_path / "other.db").exists()
    finally:
        app.session.close()


def test_root_cli_explicit_database_never_initializes_environment_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apflow.app import create_app

    chosen = tmp_path / "cli-chosen.db"
    host = tmp_path / "host-application.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{host}")
    applications = []

    def record_application(**options: Any) -> ApflowApp:
        application = create_app(**options)
        applications.append(application)
        return application

    monkeypatch.setattr("apflow.app.create_app", record_application)
    monkeypatch.setattr("apflow.api.serve_rest", lambda *args, **kwargs: None)
    try:
        result = CliRunner().invoke(_build_cli(), ["rest", "--db", f"sqlite:///{chosen}"])
        assert result.exit_code == 0, result.output
        assert len(applications) == 1
        assert chosen.exists()
        assert not host.exists()
    finally:
        for application in applications:
            application.session.close()
