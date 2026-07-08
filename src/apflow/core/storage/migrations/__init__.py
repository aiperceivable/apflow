"""
Schema migration base classes and registry

This module provides the framework for managing schema migrations.
Each migration should inherit from Migration and implement upgrade/downgrade.

Migration files are named using the pattern: {id}_{description}.py
where id is auto-extracted as the unique migration identifier.

Example:
    001_add_task_tree_fields.py
    002_add_user_fields.py
    003_rename_column.py

The id is automatically extracted from the filename and used as the unique
identifier for tracking which migrations have been applied.
"""

from abc import ABC, abstractmethod
from typing import Optional
from sqlalchemy import Engine


class Migration(ABC):
    """Base class for schema migrations

    The migration id is automatically extracted from the filename.
    Subclasses only need to implement upgrade() and downgrade() methods.
    """

    # Migration description (explain what this migration does)
    description: str = ""

    # Optional list of alias IDs for this migration (for renaming migrations)
    aliases = []

    def __init__(self) -> None:
        self._filename_id: Optional[str] = None

    def set_filename_id(self, filename_id: str) -> None:
        """Set the migration id extracted from its filename.

        Called by MigrationManager during discovery so migrations sort and
        execute in filename order (001, 002, ...) rather than by class name.
        """
        self._filename_id = filename_id

    @property
    def id(self) -> str:
        """Migration ID (extracted from filename; falls back to class name
        until set_filename_id() is called by MigrationManager during discovery)."""
        if self._filename_id:
            return self._filename_id
        return self.__class__.__name__

    @abstractmethod
    def upgrade(self, engine: Engine) -> None:
        """
        Execute migration upgrade

        Args:
            engine: SQLAlchemy engine instance

        Raises:
            Exception: If migration fails
        """
        pass

    @abstractmethod
    def downgrade(self, engine: Engine) -> None:
        """
        Execute migration downgrade (rollback)

        Args:
            engine: SQLAlchemy engine instance

        Raises:
            Exception: If downgrade fails
        """
        pass

    def __repr__(self) -> str:
        return f"<Migration(id='{self.id}')>"
