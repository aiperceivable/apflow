"""
Base Tool class for all tools.

Standalone implementation using Pydantic BaseModel.
"""

from abc import ABC
from typing import Any

from pydantic import BaseModel, Field


class BaseTool(BaseModel, ABC):
    """Base class for all tools.

    Subclasses should implement _run() for synchronous execution
    and optionally _arun() for asynchronous execution.
    """

    name: str = Field(..., description="Tool name")
    description: str = Field(..., description="Tool description")

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        """Synchronous execution — must be implemented by subclass."""
        raise NotImplementedError("Subclass must implement _run()")

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        """Asynchronous execution — defaults to calling _run()."""
        return self._run(*args, **kwargs)


__all__ = ["BaseTool"]
