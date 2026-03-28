"""
SQLite dialect configuration (default)
"""

import json
from pathlib import Path
from typing import Any, Dict


class SQLiteDialect:
    """SQLite dialect configuration (default embedded storage)"""

    @staticmethod
    def normalize_data(data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize data before writing to database.
        SQLite stores JSON as TEXT, so dicts/lists must be serialized.
        """
        normalized = {}
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                normalized[key] = json.dumps(value)
            else:
                normalized[key] = value
        return normalized

    @staticmethod
    def denormalize_data(data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Denormalize data after reading from database.
        Parse JSON strings back to Python objects.
        """
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

    @staticmethod
    def get_connection_string(path: str = ":memory:") -> str:
        """
        Generate SQLite connection string.

        Args:
            path: Database path. Accepted values:
                - ":memory:" for in-memory database
                - "file:shared?mode=memory&cache=shared&uri=true" for shared in-memory
                - "/path/to/db" for file-based database

        Returns:
            SQLAlchemy connection string (e.g. "sqlite:///:memory:")

        Raises:
            ValueError: If path is empty
        """
        if not path:
            raise ValueError("Database path must not be empty")

        if path == ":memory:":
            return "sqlite:///:memory:"
        elif path.startswith("file:"):
            return f"sqlite:///{path}"
        else:
            abs_path = str(Path(path).absolute())
            return f"sqlite:///{abs_path}"

    @staticmethod
    def get_engine_kwargs() -> Dict[str, Any]:
        """SQLite specific engine parameters."""
        return {
            "pool_pre_ping": True,
        }

    @staticmethod
    def get_pragma_statements() -> list[str]:
        """
        Return PRAGMA statements for optimal SQLite performance.

        - journal_mode=WAL: Write-Ahead Logging for concurrent reads
        - synchronous=NORMAL: Balance between safety and speed
        - cache_size=-64000: 64MB page cache
        - foreign_keys=ON: Enforce foreign key constraints
        - busy_timeout=5000: Wait 5s on lock contention
        """
        return [
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA cache_size=-64000",
            "PRAGMA foreign_keys=ON",
            "PRAGMA busy_timeout=5000",
        ]
