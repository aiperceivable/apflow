"""
Storage extension base interface

Storage extensions provide database backend implementations.
They extend the Extension interface and are registered with ExtensionCategory.STORAGE.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any
from apflow.core.extensions.base import Extension
from apflow.core.extensions.types import ExtensionCategory


class StorageBackend(Extension, ABC):
    """
    Base interface for storage backend extensions

    Storage backends provide database connection and configuration.
    They are registered with ExtensionCategory.STORAGE.

    Example:
        @storage_register()
        class SQLiteStorage(StorageBackend):
            id = "sqlite"
            name = "SQLite Storage"
            type = "sqlite"

            def get_connection_string(self, **kwargs) -> str:
                ...
    """

    @property
    def category(self) -> ExtensionCategory:
        """Extension category - always STORAGE for StorageBackend"""
        return ExtensionCategory.STORAGE

    @abstractmethod
    def normalize_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize data before writing to database

        Args:
            data: Raw data dictionary

        Returns:
            Normalized data dictionary
        """
        pass

    @abstractmethod
    def denormalize_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Denormalize data after reading from database

        Args:
            data: Normalized data from database

        Returns:
            Denormalized data dictionary
        """
        pass

    @abstractmethod
    def get_connection_string(self, **kwargs) -> str:
        """
        Generate database connection string

        Args:
            **kwargs: Connection parameters (path, user, password, host, port, database, etc.)

        Returns:
            Connection string for SQLAlchemy
        """
        pass

    def get_engine_kwargs(self) -> Dict[str, Any]:
        """
        Get SQLAlchemy engine kwargs

        Returns:
            Dictionary of engine parameters
        """
        return {}


__all__ = ["StorageBackend"]
