"""
Project detection utilities for locating project root and data directories.

This module provides shared utilities for detecting project context across
both CLI and storage modules, ensuring consistent behavior for project-local
configuration and data storage.
"""

from pathlib import Path
from typing import Optional

from apflow.logger import get_logger

logger = get_logger(__name__)


def get_project_root() -> Optional[Path]:
    """
    Find project root by looking for pyproject.toml or .git directory.

    Walks up the directory tree from current working directory
    until it finds a project marker or reaches filesystem root.

    Returns:
        Project root path if found, None otherwise

    Examples:
        >>> root = get_project_root()
        >>> if root:
        ...     print(f"Found project at: {root}")
    """
    current = Path.cwd()

    # Walk up the directory tree
    for parent in [current] + list(current.parents):
        # Check for project markers
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            logger.debug(f"Found project root: {parent}")
            return parent

    logger.debug("No project root found")
    return None


def get_project_data_dir() -> Optional[Path]:
    """
    Get project-local data directory if in project context.

    This is the standard location for project-specific data files like
    databases, cache, and other runtime data.

    Returns:
        <project_root>/.data if in project, None otherwise

    Examples:
        >>> data_dir = get_project_data_dir()
        >>> if data_dir:
        ...     db_path = data_dir / "apflow.db"
    """
    project_root = get_project_root()
    if project_root:
        data_dir = project_root / ".data"
        logger.debug(f"Project data directory: {data_dir}")
        return data_dir

    logger.debug("Not in project context, no project data directory")
    return None


def is_in_project_context() -> bool:
    """
    Check if currently running in a project context.

    Returns:
        True if in a project (has pyproject.toml or .git), False otherwise

    Examples:
        >>> if is_in_project_context():
        ...     print("Running in project context")
    """
    return get_project_root() is not None
