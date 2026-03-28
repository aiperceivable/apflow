"""
SQLite storage backend extension

Provides SQLite database backend as an ExtensionCategory.STORAGE extension.
"""

import json
from pathlib import Path
from typing import Any, Dict

from apflow.core.extensions.decorators import storage_register
from apflow.core.extensions.storage import StorageBackend


@storage_register()
class SQLiteStorage(StorageBackend):
    """
    SQLite storage backend extension

    Provides embedded SQLite database support with WAL mode.
    Registered as ExtensionCategory.STORAGE extension.
    """

    id = "sqlite"
    name = "SQLite Storage"
    description = "Embedded SQLite database backend (default)"
    version = "1.0.0"

    @property
    def type(self) -> str:
        """Extension type identifier"""
        return "sqlite"

    def normalize_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize data before writing to database."""
        normalized = {}
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                normalized[key] = json.dumps(value)
            else:
                normalized[key] = value
        return normalized

    def denormalize_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Denormalize data after reading from database."""
        denormalized = {}
        for key, value in data.items():
            if isinstance(value, str):
                try:
                    denormalized[key] = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    denormalized[key] = value
            else:
                denormalized[key] = value
        return denormalized

    def get_connection_string(self, **kwargs) -> str:
        """
        Generate SQLite connection string.

        Args:
            **kwargs: Connection parameters
                - path: Database file path (default: ":memory:")
                - connection_string: Direct connection string (if provided, used as-is)

        Returns:
            Connection string for SQLAlchemy
        """
        if "connection_string" in kwargs:
            return kwargs["connection_string"]

        path = kwargs.get("path", ":memory:")
        if path == ":memory:":
            return "sqlite:///:memory:"
        else:
            abs_path = str(Path(path).absolute())
            return f"sqlite:///{abs_path}"

    def get_engine_kwargs(self) -> Dict[str, Any]:
        """SQLite specific engine parameters."""
        return {
            "pool_pre_ping": True,
        }


__all__ = ["SQLiteStorage"]
