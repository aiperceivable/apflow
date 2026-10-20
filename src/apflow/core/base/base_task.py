"""
Base task class with common implementations

Provides common functionality for executable tasks. You can inherit from
BaseTask to get common implementations, or implement ExecutableTask directly
for maximum flexibility.
"""

import weakref
from typing import Dict, Any, Optional, Callable, Type, Union

from pydantic import BaseModel

from apflow.core.interfaces.executable_task import ExecutableTask
from apflow.core.utils.helpers import (
    validate_input_schema as _validate_input_schema,
    check_input_schema as _check_input_schema,
    resolve_schema_refs,
)


class BaseTask(ExecutableTask):
    """
    Base task class with common implementations (optional base class)

    Provides common functionality for executable tasks.
    You can inherit from BaseTask to get common implementations, or implement ExecutableTask
    directly for maximum flexibility.

    Inherit from BaseTask if you need:
    - Common initialization and input management
    - Streaming context support
    - Input validation utilities

    Implement ExecutableTask directly if you want:
    - Full control over implementation
    - Minimal dependencies
    """

    # Task definition properties - should be overridden by subclasses
    id: str = ""
    name: str = ""
    description: str = ""
    tags: list[str] = []
    examples: list[str] = []

    # Task context management - uses weak reference to avoid memory leaks
    #
    # Implementation:
    # - Uses weak reference (_task_ref) to avoid holding strong reference to task object
    # - Task object can be garbage collected when no other strong references exist
    # - TaskManager maintains strong reference during execution, so task won't be collected prematurely
    #
    # Future extension (scheme 2):
    # - Can be extended to support task_id-based storage (database/Redis)
    # - task_id property is already available for future use
    _task_ref: Optional[weakref.ref] = None

    # Task ID for future extension (scheme 2: task_id-based storage)
    # Currently not used, but available for future implementation of Redis/database-backed task storage
    task_id: Optional[str] = None

    # Internal storage for user_id (only used as fallback when task is not available)
    _user_id: Optional[str] = None

    # Cancellation support
    # Set to True if executor supports cancellation during execution
    # Set to False if executor cannot be cancelled once execution starts
    cancelable: bool = False

    # Input schema for validation (can be Pydantic BaseModel or JSON schema dict)
    inputs_schema: Optional[Union[Type[BaseModel], Dict[str, Any]]] = None

    # Output schema for result validation (can be Pydantic BaseModel or JSON schema dict)
    outputs_schema: Optional[Union[Type[BaseModel], Dict[str, Any]]] = None

    def __init__(
        self,
        inputs: Optional[Dict[str, Any]] = None,
        inputs_schema: Optional[Union[Type[BaseModel], Dict[str, Any]]] = None,
        outputs_schema: Optional[Union[Type[BaseModel], Dict[str, Any]]] = None,
        **kwargs: Any,
    ):
        """
        Initialize BaseTask

        Args:
            inputs: Initial input parameters
            inputs_schema: Optional input schema for validation (Pydantic BaseModel or JSON schema dict)
            outputs_schema: Optional output schema for result validation (Pydantic BaseModel or JSON schema dict)
            **kwargs: Additional configuration options
                - task_id: Task ID for cancellation checking (optional)
        """
        self.inputs: Dict[str, Any] = inputs or {}

        # Set input schema if provided
        if inputs_schema is not None:
            self.inputs_schema = inputs_schema

        # Set output schema if provided
        if outputs_schema is not None:
            self.outputs_schema = outputs_schema

        # Streaming context for progress updates
        self.event_queue = None
        self.context = None

        # Cancellation checker callback (set by TaskManager)
        # Executor calls this function to check if task is cancelled
        # Returns True if cancelled, False otherwise
        self.cancellation_checker: Optional[Callable[[], bool]] = kwargs.get("cancellation_checker")

        # Initialize with any provided kwargs
        self.init(**kwargs)

    def init(self, **kwargs: Any) -> None:
        """Initialize task with configuration"""
        if "id" in kwargs:
            self.id = kwargs["id"]
        if "name" in kwargs:
            self.name = kwargs["name"]
        if "description" in kwargs:
            self.description = kwargs["description"]
        if "tags" in kwargs:
            self.tags = kwargs["tags"]
        if "examples" in kwargs:
            self.examples = kwargs["examples"]
        if "inputs" in kwargs:
            self.inputs = kwargs["inputs"]
        if "task" in kwargs:
            # Store task context using weak reference (scheme 1: weak reference)
            # Also store task_id for future extension (scheme 2: task_id-based storage)
            task_obj = kwargs["task"]
            self.task = task_obj  # Use property setter to set weak reference
            if task_obj and hasattr(task_obj, "id"):
                self.task_id = task_obj.id  # Store task_id for future use
        if "task_id" in kwargs:
            # Store task_id directly (for future scheme 2 implementation)
            self.task_id = kwargs["task_id"]
        if "user_id" in kwargs:
            # Store user_id as fallback (only used when task is not available)
            self._user_id = kwargs["user_id"]
        if "cancellation_checker" in kwargs:
            self.cancellation_checker = kwargs["cancellation_checker"]
        if "cancelable" in kwargs:
            self.cancelable = kwargs["cancelable"]
        if "inputs_schema" in kwargs:
            self.inputs_schema = kwargs["inputs_schema"]
        if "outputs_schema" in kwargs:
            self.outputs_schema = kwargs["outputs_schema"]

    def set_inputs(self, inputs: Dict[str, Any]) -> None:
        """
        Set input parameters

        Args:
            inputs: Dictionary of inputs
        """
        self.inputs = inputs

    def set_streaming_context(self, event_queue: Any, context: Any) -> None:
        """
        Set streaming context for progress updates

        Args:
            event_queue: Event queue for streaming updates
            context: Request context
        """
        self.event_queue = event_queue
        self.context = context

    def get_input_schema(self) -> Dict[str, Any]:
        """
        Return input parameter schema (JSON Schema format)

        Supports both Pydantic BaseModel classes and raw dict schemas.
        When a Pydantic BaseModel is used, model_json_schema() is called
        and $ref references are resolved inline via resolve_schema_refs().

        Returns:
            JSON Schema dictionary, or empty dict if no schema defined
        """
        if self.inputs_schema is None:
            return {}
        if isinstance(self.inputs_schema, dict):
            return self.inputs_schema
        return resolve_schema_refs(self.inputs_schema.model_json_schema())

    def get_output_schema(self) -> Dict[str, Any]:
        """
        Return output result schema (JSON Schema format)

        Supports both Pydantic BaseModel classes and raw dict schemas.
        When a Pydantic BaseModel is used, model_json_schema() is called
        and $ref references are resolved inline via resolve_schema_refs().

        Returns:
            JSON Schema dictionary, or empty dict if no schema defined
        """
        if self.outputs_schema is None:
            return {}
        if isinstance(self.outputs_schema, dict):
            return self.outputs_schema
        return resolve_schema_refs(self.outputs_schema.model_json_schema())

    def validate_input_schema(self, parameters: Dict[str, Any]) -> bool:
        """
        Validate parameters using input schema

        Args:
            parameters: Parameters to validate

        Returns:
            True if valid, False otherwise
        """
        if self.inputs_schema:
            return _validate_input_schema(self.inputs_schema, parameters)
        return True

    def check_input_schema(self, parameters: Dict[str, Any]) -> None:
        """
        Check parameters using input schema (raises exception if invalid)

        Args:
            parameters: Parameters to check

        Raises:
            ValueError: If validation fails
        """
        if self.inputs_schema:
            _check_input_schema(self.inputs_schema, parameters)

    def get_demo_result(self, task: Any, inputs: Dict[str, Any]) -> Any:
        """
        Optional method to provide custom demo data for this executor

        This method is called when TaskManager is configured with use_demo=True.
        Executors can override this method to provide realistic demo data
        that matches their actual execution results.

        Args:
            task: TaskModelTypel instance for this task
            inputs: Input parameters for the task

        Returns:
            Business example, independent of result_format. None selects the
            default example. TaskManager wraps the example once and adds a
            demo_mode business marker to dictionary examples.

        Example:
            def get_demo_result(self, task, inputs):
                return {
                    "status": "completed",
                    "result": "Demo execution result",
                    "data": {"processed": True}
                }
        """
        # Default implementation: return None to use default demo data
        return None

    @property
    def task(self) -> Optional[Any]:
        """
        Get task object from weak reference (scheme 1)

        This property provides access to task object while avoiding memory leaks.
        Uses weak reference so task object can be garbage collected when no other
        strong references exist. TaskManager maintains strong reference during execution.

        Future extension (scheme 2):
        - Can be extended to load from database/Redis using task_id
        - Would require TaskRepository or Redis client access

        Returns:
            TaskModelTypel instance, or None if not available or garbage collected
        """
        if self._task_ref is None:
            return None
        task = self._task_ref()
        if task is None:
            # Weak reference was garbage collected, clear it
            self._task_ref = None
        return task

    @task.setter
    def task(self, value: Optional[Any]) -> None:
        """
        Set task object using weak reference (scheme 1)

        This avoids memory leaks by not holding a strong reference to the task object.
        The task object will be garbage collected when no other strong references exist.
        TaskManager maintains a strong reference during execution, so the task won't be
        collected prematurely.

        Args:
            value: TaskModelTypel instance, or None to clear
        """
        if value is None:
            self._task_ref = None
        else:
            self._task_ref = weakref.ref(value)
            # Also store task_id for future extension (scheme 2)
            if hasattr(value, "id"):
                self.task_id = value.id

    @property
    def user_id(self) -> Optional[str]:
        """
        Get user_id from task context (preferred) or fallback to _user_id

        This property provides convenient access to user_id:
        - First tries to get from task.user_id (if task is available)
        - Falls back to _user_id (if task is not available, e.g., in tests or special scenarios)

        Returns:
            User ID string, or None if not available
        """
        task = self.task  # Use property to get from weak ref
        if task and hasattr(task, "user_id"):
            return task.user_id
        return self._user_id

    @user_id.setter
    def user_id(self, value: Optional[str]) -> None:
        """
        Set user_id (stores in _user_id as fallback)

        Note: If task is available, prefer setting task.user_id directly.
        This setter is mainly for backward compatibility and special scenarios.
        """
        self._user_id = value

    def clear_task_context(self) -> None:
        """
        Clear task context reference (explicit cleanup method)

        This method should be called when the executor is done with the task.
        TaskManager automatically calls this when removing executor from _executor_instances.

        This is useful for:
        - Explicit cleanup in long-running executors
        - Memory management in special scenarios
        - Testing and debugging
        - Preventing memory leaks

        Note:
        - Clearing task context does not affect the actual task in the database
        - task_id is preserved for future extension (scheme 2: task_id-based storage)
        - _user_id is kept as fallback (it's a simple string, not a reference)
        """
        self._task_ref = None
        # Keep task_id for future extension (scheme 2)
        # Keep _user_id as fallback (it's a simple string, not a reference)


__all__ = ["BaseTask"]
