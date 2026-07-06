"""Tests for apflow app bootstrap and CLI entry points."""

from click.testing import CliRunner

from apflow.app import ApflowApp, create_app
from apflow.cli import cli as _get_cli

cli = _get_cli()


class TestCreateApp:
    def test_creates_app_with_memory_db(self):
        app = create_app(connection_string="sqlite:///:memory:")
        assert isinstance(app, ApflowApp)
        assert app.session is not None
        assert app.task_manager is not None
        assert app.task_creator is not None
        assert app.task_repository is not None
        assert app.registry is not None

    def test_registry_has_modules(self):
        app = create_app(connection_string="sqlite:///:memory:")
        modules = list(app.registry.list())
        assert len(modules) > 5  # At least 5 task modules + executors

    def test_registry_has_task_modules(self):
        app = create_app(connection_string="sqlite:///:memory:")
        modules = list(app.registry.list())
        assert "apflow.task.create" in modules
        assert "apflow.task.execute" in modules
        assert "apflow.task.list" in modules
        assert "apflow.task.get" in modules
        assert "apflow.task.delete" in modules


class TestCLI:
    def test_cli_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "AI-Perceivable Distributed Orchestration" in result.output

    def test_serve_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--host" in result.output
        assert "--port" in result.output
        assert "--explorer" in result.output

    def test_mcp_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["mcp", "--help"])
        assert result.exit_code == 0
        assert "--transport" in result.output
        assert "stdio" in result.output
        assert "streamable-http" in result.output

    def test_info_command(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["info"])
        assert result.exit_code == 0
        assert "apflow" in result.output
        assert "Storage" in result.output

    def test_version(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "0.22" in result.output
