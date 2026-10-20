"""
Tests for project detection utilities.
"""

from unittest.mock import patch

import pytest

from apflow.core.utils.project_detection import (
    get_project_data_dir,
    get_project_root,
    is_in_project_context,
)


@pytest.fixture
def temp_project_dir(tmp_path):
    """Create a temporary project directory with pyproject.toml."""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").touch()
    return project_dir


@pytest.fixture
def temp_git_dir(tmp_path):
    """Create a temporary directory with .git marker."""
    project_dir = tmp_path / "git_project"
    project_dir.mkdir()
    (project_dir / ".git").mkdir()
    return project_dir


@pytest.fixture
def temp_non_project_dir(tmp_path):
    """Create a temporary directory without project markers."""
    non_project_dir = tmp_path / "non_project"
    non_project_dir.mkdir()
    return non_project_dir


class TestGetProjectRoot:
    """Tests for get_project_root function."""

    def test_finds_project_root_with_pyproject_toml(self, temp_project_dir):
        """Test that project root is found when pyproject.toml exists."""
        subdir = temp_project_dir / "src" / "package"
        subdir.mkdir(parents=True)

        with patch("pathlib.Path.cwd", return_value=subdir):
            result = get_project_root()
            assert result == temp_project_dir

    def test_finds_project_root_with_git(self, temp_git_dir):
        """Test that project root is found when .git directory exists."""
        subdir = temp_git_dir / "src"
        subdir.mkdir()

        with patch("pathlib.Path.cwd", return_value=subdir):
            result = get_project_root()
            assert result == temp_git_dir

    def test_returns_none_when_no_project_markers(self, temp_non_project_dir):
        """Test that None is returned when no project markers found."""
        with patch("pathlib.Path.cwd", return_value=temp_non_project_dir):
            result = get_project_root()
            assert result is None

    def test_finds_root_from_deeply_nested_directory(self, temp_project_dir):
        """Test finding project root from deeply nested subdirectory."""
        deep_dir = temp_project_dir / "a" / "b" / "c" / "d"
        deep_dir.mkdir(parents=True)

        with patch("pathlib.Path.cwd", return_value=deep_dir):
            result = get_project_root()
            assert result == temp_project_dir


class TestGetProjectDataDir:
    """Tests for get_project_data_dir function."""

    def test_returns_data_dir_in_project(self, temp_project_dir):
        """Test that .data directory path is returned when in project."""
        with patch("pathlib.Path.cwd", return_value=temp_project_dir):
            result = get_project_data_dir()
            assert result == temp_project_dir / ".data"

    def test_returns_none_when_not_in_project(self, temp_non_project_dir):
        """Test that None is returned when not in project context."""
        with patch("pathlib.Path.cwd", return_value=temp_non_project_dir):
            result = get_project_data_dir()
            assert result is None

    def test_data_dir_path_structure(self, temp_project_dir):
        """Test that data directory has correct path structure."""
        subdir = temp_project_dir / "src"
        subdir.mkdir()

        with patch("pathlib.Path.cwd", return_value=subdir):
            result = get_project_data_dir()
            assert result == temp_project_dir / ".data"
            assert result is not None
            assert result.name == ".data"


class TestIsInProjectContext:
    """Tests for is_in_project_context function."""

    def test_returns_true_in_project(self, temp_project_dir):
        """Test that True is returned when in project context."""
        with patch("pathlib.Path.cwd", return_value=temp_project_dir):
            assert is_in_project_context() is True

    def test_returns_false_outside_project(self, temp_non_project_dir):
        """Test that False is returned when not in project context."""
        with patch("pathlib.Path.cwd", return_value=temp_non_project_dir):
            assert is_in_project_context() is False

    def test_returns_true_in_git_project(self, temp_git_dir):
        """Test that True is returned when in git project."""
        with patch("pathlib.Path.cwd", return_value=temp_git_dir):
            assert is_in_project_context() is True
