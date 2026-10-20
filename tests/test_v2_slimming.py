"""Post-slimming verification tests for apflow v2"""

import pytest
from importlib import import_module
import tomllib
from pathlib import Path


class TestPreservedImports:
    """All preserved public imports still work."""

    def test_core_interfaces(self):
        from apflow import ExecutableTask, BaseTask

        assert ExecutableTask is not None
        assert BaseTask is not None

    def test_core_execution(self):
        from apflow import TaskManager, StreamingCallbacks

        assert TaskManager is not None
        assert StreamingCallbacks is not None

    def test_core_storage(self):
        from apflow import create_session, get_default_session

        assert create_session is not None
        assert get_default_session is not None

    def test_decorators(self):
        from apflow import executor_register, storage_register, hook_register

        assert executor_register is not None
        assert storage_register is not None
        assert hook_register is not None

    def test_task_creator(self):
        from apflow.core.execution.task_creator import TaskCreator

        assert TaskCreator is not None

    def test_task_repository(self):
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        assert TaskRepository is not None

    def test_extension_scanner(self):
        from apflow.core.extensions.scanner import ExtensionScanner

        assert ExtensionScanner is not None

    def test_version(self) -> None:
        from apflow import __version__

        project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
        assert __version__ == project["project"]["version"]


class TestDeletedImportsFail:
    """Deleted module imports raise ImportError."""

    def test_api_a2a_deleted(self):
        with pytest.raises(ImportError):
            import_module("apflow.api.a2a.agent_executor")

    def test_cli_deleted(self):
        with pytest.raises(ImportError):
            import_module("apflow.cli.main")

    def test_crewai_deleted(self):
        with pytest.raises(ImportError):
            import_module("apflow.extensions.crewai.crewai_executor")

    def test_generate_deleted(self):
        with pytest.raises(ImportError):
            import_module("apflow.extensions.generate.generate_executor")

    def test_grpc_deleted(self):
        with pytest.raises(ImportError):
            import_module("apflow.extensions.grpc.grpc_executor")

    def test_llm_deleted(self):
        with pytest.raises(ImportError):
            import_module("apflow.extensions.llm.llm_executor")

    def test_tools_deleted(self):
        with pytest.raises(ImportError):
            import_module("apflow.extensions.tools.limited_scrape_tools")


class TestNoDuckDBReferences:
    """No DuckDB dependency remains."""

    def test_sqlite_is_default_dialect(self):
        from apflow.core.storage.dialects.registry import get_dialect_config

        # SQLite should be registered
        config = get_dialect_config("sqlite")
        assert config is not None

    def test_duckdb_dialect_not_registered(self):
        from apflow.core.storage.dialects.registry import get_dialect_config

        with pytest.raises(ValueError, match="Unsupported dialect"):
            get_dialect_config("duckdb")
