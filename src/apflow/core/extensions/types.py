"""
Extension category types

Defines the categories of extensions supported by the framework.

Design Principles:
- Only define categories that are actively used or have clear implementation plans
- Follow YAGNI (You Aren't Gonna Need It) - don't add categories until needed
- Keep categories focused on core extension types

Reference: Inspired by VS Code's contribution points and Django's app-based plugins.
"""

from enum import Enum


class ExtensionCategory(str, Enum):
    """
    Extension categories for classification

    Each extension must belong to one category, which determines
    how it's used and discovered in the system.

    Categories are kept minimal - only those with active use cases or
    clear implementation plans are included. Additional categories can
    be added as needed.
    """

    EXECUTOR = "executor"
    """
    Task execution implementations
    
    Examples:
    - stdio: Command and system info executors
    - crewai: LLM-based agent crew execution
    - http: HTTP API call executors
    - custom: User-defined task executors
    """

    STORAGE = "storage"
    """
    Storage backend implementations
    
    Examples:
    - sqlite: SQLite embedded database
    - postgresql: PostgreSQL database
    
    Registered via @storage_register() decorator.
    """

    HOOK = "hook"
    """
    Lifecycle hook implementations
    
    Examples:
    - pre_execution: Hooks executed before task execution
    - post_execution: Hooks executed after task execution
    
    Registered via @hook_register() decorator.
    """


__all__ = ["ExtensionCategory"]
