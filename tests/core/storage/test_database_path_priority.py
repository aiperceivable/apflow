"""
Tests for database path resolution with project-aware priority.

Note: DATABASE_URL and APFLOW_DATABASE_URL take precedence over file paths.
These tests focus on the file path resolution when connection strings are not set.
"""

import logging
import os
from unittest.mock import patch

import pytest

from apflow.core.storage.factory import _get_default_db_path


@pytest.fixture
def reset_logging():
    """Reset logging configuration to ensure clean state for tests."""
    # Get the factory logger and ensure DEBUG level is not permanently set
    factory_logger = logging.getLogger("apflow.core.storage.factory")
    original_level = factory_logger.level
    original_handlers = factory_logger.handlers.copy()

    yield

    # Restore original logging state
    factory_logger.setLevel(original_level)
    for handler in factory_logger.handlers[:]:
        if handler not in original_handlers:
            factory_logger.removeHandler(handler)


@pytest.fixture
def temp_project_with_pyproject(tmp_path):
    """Create a temporary project directory with pyproject.toml."""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").touch()
    return project_dir


@pytest.fixture
def temp_non_project_dir(tmp_path):
    """Create a temporary directory without project markers."""
    non_project_dir = tmp_path / "non_project"
    non_project_dir.mkdir()
    return non_project_dir


@pytest.fixture
def mock_home_dir(tmp_path):
    """Mock home directory for testing."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    return home_dir


class TestDefaultDbPathPriority:
    """Tests for _get_default_db_path function priority logic.

    Note: These tests assume DATABASE_URL is not set. When DATABASE_URL is set,
    it takes precedence over file path resolution.
    """

    def test_new_project_uses_project_local_path(self, temp_project_with_pyproject, mock_home_dir):
        """Test that new projects without existing db use project-local .data/apflow.db."""
        with patch("pathlib.Path.cwd", return_value=temp_project_with_pyproject):
            with patch("pathlib.Path.home", return_value=mock_home_dir):
                with patch.dict(os.environ, {}, clear=True):
                    result = _get_default_db_path()
                    expected = str(temp_project_with_pyproject / ".data" / "apflow.db")
                    assert result == expected

    def test_existing_project_local_db_is_used(self, temp_project_with_pyproject, mock_home_dir):
        """Test that existing project-local database is preferred."""
        # Create project-local database
        data_dir = temp_project_with_pyproject / ".data"
        data_dir.mkdir()
        project_db = data_dir / "apflow.db"
        project_db.touch()

        with patch("pathlib.Path.cwd", return_value=temp_project_with_pyproject):
            with patch("pathlib.Path.home", return_value=mock_home_dir):
                with patch.dict(os.environ, {}, clear=True):
                    result = _get_default_db_path()
                    assert result == str(project_db)

    def test_existing_legacy_db_is_used_when_no_project_local(
        self, temp_project_with_pyproject, mock_home_dir
    ):
        """Test backward compatibility: legacy database is used when project-local doesn't exist."""
        # Create legacy database location
        legacy_dir = mock_home_dir / ".aiperceivable" / "data"
        legacy_dir.mkdir(parents=True)
        legacy_db = legacy_dir / "apflow.db"
        legacy_db.touch()

        with patch("pathlib.Path.cwd", return_value=temp_project_with_pyproject):
            with patch("pathlib.Path.home", return_value=mock_home_dir):
                with patch.dict(os.environ, {}, clear=True):
                    result = _get_default_db_path()
                    assert result == str(legacy_db)

    def test_project_local_preferred_when_both_exist(
        self, temp_project_with_pyproject, mock_home_dir
    ):
        """Test that project-local database is preferred when both locations exist."""
        # Create both databases
        data_dir = temp_project_with_pyproject / ".data"
        data_dir.mkdir()
        project_db = data_dir / "apflow.db"
        project_db.touch()

        legacy_dir = mock_home_dir / ".aiperceivable" / "data"
        legacy_dir.mkdir(parents=True)
        legacy_db = legacy_dir / "apflow.db"
        legacy_db.touch()

        with patch("pathlib.Path.cwd", return_value=temp_project_with_pyproject):
            with patch("pathlib.Path.home", return_value=mock_home_dir):
                with patch.dict(os.environ, {}, clear=True):
                    result = _get_default_db_path()
                    assert result == str(project_db)

    def test_outside_project_uses_global_location(self, temp_non_project_dir, mock_home_dir):
        """Test that global location is used when not in project context."""
        with patch("pathlib.Path.cwd", return_value=temp_non_project_dir):
            with patch("pathlib.Path.home", return_value=mock_home_dir):
                with patch.dict(os.environ, {}, clear=True):
                    result = _get_default_db_path()
                    expected = str(mock_home_dir / ".aiperceivable" / "data" / "apflow.db")
                    assert result == expected

    def test_global_location_directory_is_created(self, temp_non_project_dir, mock_home_dir):
        """Test that global data directory is created if it doesn't exist."""
        with patch("pathlib.Path.cwd", return_value=temp_non_project_dir):
            with patch("pathlib.Path.home", return_value=mock_home_dir):
                with patch.dict(os.environ, {}, clear=True):
                    _get_default_db_path()

                    # Verify directory was created
                    expected_dir = mock_home_dir / ".aiperceivable" / "data"
                    assert expected_dir.exists()
                    assert expected_dir.is_dir()

    def test_project_data_directory_is_created_for_new_project(
        self, temp_project_with_pyproject, mock_home_dir
    ):
        """Test that .data directory is created for new projects."""
        with patch("pathlib.Path.cwd", return_value=temp_project_with_pyproject):
            with patch("pathlib.Path.home", return_value=mock_home_dir):
                with patch.dict(os.environ, {}, clear=True):
                    _get_default_db_path()

                    # Verify .data directory was created
                    data_dir = temp_project_with_pyproject / ".data"
                    assert data_dir.exists()
                    assert data_dir.is_dir()

    def test_subdirectory_finds_project_root_database(
        self, temp_project_with_pyproject, mock_home_dir
    ):
        """Test that database path is correctly resolved from subdirectory."""
        subdir = temp_project_with_pyproject / "src" / "package"
        subdir.mkdir(parents=True)

        # Create project-local database
        data_dir = temp_project_with_pyproject / ".data"
        data_dir.mkdir()
        project_db = data_dir / "apflow.db"
        project_db.touch()

        with patch("pathlib.Path.cwd", return_value=subdir):
            with patch("pathlib.Path.home", return_value=mock_home_dir):
                with patch.dict(os.environ, {}, clear=True):
                    result = _get_default_db_path()
                    assert result == str(project_db)


class TestDatabaseUrlEnvironmentVariables:
    """Tests for DATABASE_URL environment variable handling.

    Note: DATABASE_URL is handled by _get_database_url_from_env() and takes
    precedence before _get_default_db_path() is even called.
    """

    def test_database_url_doc_reference(self):
        """Document that DATABASE_URL should be tested via create_session/get_default_session."""
        # DATABASE_URL handling is tested in the main session creation flow
        # It's checked before _get_default_db_path() is called
        # See: _get_database_url_from_env() function
        pass


class TestDatabasePathLogging:
    """Tests for database path logging behavior."""

    def test_logging_isolation_even_after_pollution(
        self, temp_project_with_pyproject, mock_home_dir, caplog, reset_logging
    ):
        """Test that logging isolation works even after other tests pollute logger state.

        This test simulates environment pollution by a previous test that may have
        modified the logger state, and verifies that our logging capture still works.
        """
        import logging

        # Simulate environment pollution from previous test
        factory_logger = logging.getLogger("apflow.core.storage.factory")
        # Set to WARNING level to simulate pollution
        factory_logger.setLevel(logging.WARNING)

        # Now verify that caplog.at_level() properly overrides this
        with caplog.at_level(logging.DEBUG, logger="apflow.core.storage.factory"):
            with patch("pathlib.Path.cwd", return_value=temp_project_with_pyproject):
                with patch("pathlib.Path.home", return_value=mock_home_dir):
                    with patch.dict(os.environ, {}, clear=True):
                        _get_default_db_path()

                        # Even with polluted logger state, caplog.at_level should capture it
                        assert any(
                            "Database path:" in record.message for record in caplog.records
                        ), f"Expected log message not found. Captured records: {[r.message for r in caplog.records]}"

    def test_debug_log_for_project_local_path(
        self, temp_project_with_pyproject, mock_home_dir, caplog, reset_logging
    ):
        """Test that debug log is emitted when using project-local path."""
        import logging

        with caplog.at_level(logging.DEBUG, logger="apflow.core.storage.factory"):
            with patch("pathlib.Path.cwd", return_value=temp_project_with_pyproject):
                with patch("pathlib.Path.home", return_value=mock_home_dir):
                    with patch.dict(os.environ, {}, clear=True):
                        _get_default_db_path()

                        # Check that debug log was emitted
                        assert any("Database path:" in record.message for record in caplog.records)
                        assert any(
                            "project-local" in record.message.lower() for record in caplog.records
                        )

    def test_debug_log_for_legacy_path(
        self, temp_project_with_pyproject, mock_home_dir, caplog, reset_logging
    ):
        """Test that debug log with tip is emitted when using legacy path."""
        import logging

        # Create legacy database
        legacy_dir = mock_home_dir / ".aiperceivable" / "data"
        legacy_dir.mkdir(parents=True)
        legacy_db = legacy_dir / "apflow.db"
        legacy_db.touch()

        with caplog.at_level(logging.DEBUG, logger="apflow.core.storage.factory"):
            with patch("pathlib.Path.cwd", return_value=temp_project_with_pyproject):
                with patch("pathlib.Path.home", return_value=mock_home_dir):
                    with patch.dict(os.environ, {}, clear=True):
                        _get_default_db_path()

                        # Check that debug log includes tip about copying
                        assert any(
                            "consider copying to" in record.message.lower()
                            for record in caplog.records
                        )


class TestDatabasePathEdgeCases:
    """Tests for edge cases in database path resolution."""

    def test_handles_symlinked_project_directory(self, tmp_path, mock_home_dir):
        """Test that symlinked project directories are handled correctly."""
        # Create actual project
        actual_project = tmp_path / "actual_project"
        actual_project.mkdir()
        (actual_project / "pyproject.toml").touch()

        # Create symlink
        symlink_project = tmp_path / "symlink_project"
        symlink_project.symlink_to(actual_project)

        with patch("pathlib.Path.cwd", return_value=symlink_project):
            with patch("pathlib.Path.home", return_value=mock_home_dir):
                with patch.dict(os.environ, {}, clear=True):
                    result = _get_default_db_path()
                    # Should work without errors
                    assert "apflow.db" in result

    def test_handles_multiple_project_markers(self, tmp_path, mock_home_dir):
        """Test behavior when both pyproject.toml and .git exist."""
        project_dir = tmp_path / "full_project"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").touch()
        (project_dir / ".git").mkdir()

        with patch("pathlib.Path.cwd", return_value=project_dir):
            with patch("pathlib.Path.home", return_value=mock_home_dir):
                with patch.dict(os.environ, {}, clear=True):
                    result = _get_default_db_path()
                    expected = str(project_dir / ".data" / "apflow.db")
                    assert result == expected

    def test_empty_env_var_is_ignored(self, temp_project_with_pyproject, mock_home_dir):
        """Test that empty APFLOW_DB_PATH environment variable is treated as not set."""
        with patch("pathlib.Path.cwd", return_value=temp_project_with_pyproject):
            with patch("pathlib.Path.home", return_value=mock_home_dir):
                with patch.dict(os.environ, {"APFLOW_DB_PATH": ""}, clear=False):
                    result = _get_default_db_path()
                    # Empty string is falsy, so should use default logic
                    expected = str(temp_project_with_pyproject / ".data" / "apflow.db")
                    assert result == expected or ".aiperceivable" in result
