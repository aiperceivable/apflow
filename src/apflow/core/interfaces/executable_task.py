"""
Executable task interface

This module defines the core interface for all executable tasks.
All task executors must implement this interface.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Dict, Optional, TYPE_CHECKING
from apflow.core.extensions.base import Extension
from apflow.core.extensions.types import ExtensionCategory
from apflow.core.execution.result_protocol import ResultFormat


class ExecutableTask(Extension, ABC):
    """

    Executable task interface - all executable units must implement this interface.

    This interface extends Extension, so all executors are registered extensions.
    Built-in implementations include:
    - RestExecutor: HTTP/REST API calls
    - AggregateResultsExecutor: Combine dependency task results
    - ApFlowApiExecutor: Inter-instance orchestration (cluster)
    - SendEmailExecutor: Email notifications

    Use @function_executor decorator for lightweight registration
    of any async/sync function as an executor.

    Optional methods for durable execution:
    - supports_checkpoint(): Whether this executor supports checkpoint/resume
    - get_checkpoint(): Serialize current execution state
    - resume_from_checkpoint(): Restore state from checkpoint
    """

    if TYPE_CHECKING:
        description: str

    result_format: ClassVar[ResultFormat] = "legacy"

    @property
    def category(self) -> ExtensionCategory:
        """Extension category - always EXECUTOR for ExecutableTask"""
        return ExtensionCategory.EXECUTOR

    if not TYPE_CHECKING:

        @property
        @abstractmethod
        def id(self) -> str:
            """Unique identifier for this task (required by Extension)"""
            pass

    if not TYPE_CHECKING:

        @property
        @abstractmethod
        def name(self) -> str:
            """Display name for this task (required by Extension)"""
            pass

    if not TYPE_CHECKING:

        @property
        @abstractmethod
        def description(self) -> str:
            """Description of what this task does (required by Extension)"""
            pass

    @abstractmethod
    async def execute(self, inputs: Dict[str, Any]) -> Any:
        """
        Execute this task

        Args:
            inputs: Input parameters for task execution

        Returns:
            The declared result_format determines the return contract:
            envelope: {result: business output, error?: string, token_usage?: counters}.
            raw: the entire return value is business output, with no reserved keys.
            legacy: preserve the entire return value and recognize top-level error/usage.
            New executors should declare envelope. The framework stores business
            output in TaskModel.result and interprets only the declared outer layer.
        """
        pass

    @abstractmethod
    def get_input_schema(self) -> Dict[str, Any]:
        """
        Return input parameter schema (JSON Schema format)

        Returns:
            JSON Schema dictionary describing input parameter structure
        """
        pass

    @abstractmethod
    def get_output_schema(self) -> Dict[str, Any]:
        """
        Return output result schema (JSON Schema format)

        Returns:
            JSON Schema dictionary describing output result structure
        """
        pass

    def supports_checkpoint(self) -> bool:
        """Whether this task supports checkpoint/resume. Default: False."""
        return False

    def get_checkpoint(self) -> Optional[Dict[str, Any]]:
        """Serialize current execution state for checkpoint. Default: None."""
        return None

    async def resume_from_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        """Restore execution state from a checkpoint. Default: no-op."""
        pass

    async def cancel(self) -> Dict[str, Any]:
        """
        Cancel task execution (optional method)

        This method is called by TaskManager when cancellation is requested.
        Executors that support cancellation should implement this method.

        Returns:
            Dictionary with cancellation result:
            {
                "status": "cancelled" | "failed",
                "message": str,  # Optional cancellation message
                "partial_result": Any,  # Optional partial result if available
                "token_usage": Dict,  # Optional token usage if available
            }

        Note:
            - If executor doesn't implement this method, TaskManager will handle cancellation
              by checking cancellation status and stopping execution at checkpoints
            - For executors that cannot be cancelled during execution,
              this method may return a result indicating cancellation will be checked after execution
        """
        # Default implementation: return not supported
        return {
            "status": "failed",
            "message": "Cancellation not supported by this executor",
            "error": "Executor does not implement cancel() method",
        }


__all__ = ["ExecutableTask"]
