"""
Base Extension interface

All extensions must implement this interface to be registered and used
in the framework.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from apflow.core.extensions.types import ExtensionCategory


class Extension(ABC):
    """
    Base interface for all extensions

    All extensions (executors, storage, hooks, etc.) must implement this interface.
    Extensions are registered by their globally unique ID and can be discovered
    by category and type.

    Attributes:
        id: Globally unique identifier (required)
        category: Extension category (required)
        name: Display name (required)
        version: Extension version (optional, default: "1.0.0")
        type: Type identifier for categorization (optional)
        metadata: Additional metadata dictionary (optional)

    Example:
        class MyExecutor(Extension, ExecutableTask):
            id = "my_executor"
            category = ExtensionCategory.EXECUTOR
            name = "My Custom Executor"
            type = "custom"

            async def execute(self, inputs):
                ...
    """

    @property
    @abstractmethod
    def id(self) -> str:
        """
        Globally unique identifier for this extension

        This ID must be unique across all extensions in the system.
        Used for precise lookup and conflict detection.

        Returns:
            Unique identifier string
        """
        pass

    @property
    @abstractmethod
    def category(self) -> ExtensionCategory:
        """
        Extension category

        Determines which category this extension belongs to.
        Used for categorization and discovery.

        Returns:
            ExtensionCategory enum value
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Display name for this extension

        Returns:
            Human-readable name string
        """
        pass

    @property
    def version(self) -> str:
        """
        Extension version

        Returns:
            Version string (default: "1.0.0")
        """
        return "1.0.0"

    @property
    def type(self) -> Optional[str]:
        """
        Type identifier for categorization (optional)

        This is used for categorization within a category.
        Examples:
        - Executor: "stdio", "crewai", "http"
        - Storage: "sqlite", "postgres", "mongodb"
        - Hook: "pre", "post", "error"

        Returns:
            Type string or None
        """
        return None

    @property
    def metadata(self) -> Dict[str, Any]:
        """
        Additional metadata dictionary (optional)

        Can contain version info, author, description, dependencies, etc.

        Returns:
            Metadata dictionary
        """
        return {}

    def __repr__(self) -> str:
        """String representation"""
        return f"<{self.__class__.__name__}(id='{self.id}', category='{self.category.value}', type='{self.type}')>"


__all__ = ["Extension"]
