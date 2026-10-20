"""
Core interfaces for apflow

This module defines the core interfaces that all implementations must follow.
Interfaces are abstract contracts that define what methods must be implemented.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apflow.core.interfaces.executable_task import (
        ExecutableTask,
    )

__all__ = [
    "ExecutableTask",
]


def __getattr__(name: str) -> Any:
    """Lazy import to avoid loading apflow.core.extensions at package import time"""
    if name == "ExecutableTask":
        from apflow.core.interfaces.executable_task import ExecutableTask

        return ExecutableTask
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
