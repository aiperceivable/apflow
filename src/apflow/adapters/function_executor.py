"""
Decorator that wraps a plain function as a registered ExecutableTask.

Generates a BaseTask subclass dynamically, registers it via executor_register,
and returns the original function unchanged.

Example:
    @function_executor(id="fetch_data", description="Fetch data from URL")
    async def fetch_data(inputs: dict) -> dict:
        return {"data": await httpx.get(inputs["url"])}
"""

import asyncio
import inspect
from typing import Any, Callable, Dict, Optional, Type, Union

from pydantic import BaseModel

from apflow.core.base import BaseTask
from apflow.core.extensions.decorators import executor_register
from apflow.logger import get_logger

logger = get_logger(__name__)

# Module-level registry so the bridge can discover function executors
_function_executor_classes: Dict[str, type] = {}


def function_executor(
    id: str,
    description: str,
    name: Optional[str] = None,
    input_schema: Optional[Dict[str, Any]] = None,
    output_schema: Optional[Dict[str, Any]] = None,
    input_model: Optional[Type[BaseModel]] = None,
    output_model: Optional[Type[BaseModel]] = None,
    tags: Optional[list[str]] = None,
    override: bool = False,
) -> Callable:
    """Decorator that registers an async/sync function as an apflow executor.

    The generated executor is discoverable by the bridge and automatically
    exposed via MCP/A2A/CLI. The decorated function itself is returned unchanged.

    Args:
        id: Unique executor identifier (used in task trees).
        description: Human-readable description (exposed to AI agents).
        name: Display name; defaults to id title-cased.
        input_schema: JSON Schema dict for inputs.
        output_schema: JSON Schema dict for outputs.
        input_model: Pydantic model for inputs (mutually exclusive with input_schema).
        output_model: Pydantic model for outputs (mutually exclusive with output_schema).
        tags: Tags for categorization.
        override: Force re-registration if id already exists.

    Returns:
        The original function, unchanged.

    Raises:
        ValueError: If id/description empty, or schema/model conflict.
    """
    if not id:
        raise ValueError("id must be non-empty")
    if not description:
        raise ValueError("description must be non-empty")

    resolved_input = _resolve_schema(input_schema, input_model)
    resolved_output = _resolve_schema(output_schema, output_model)

    def decorator(fn: Callable) -> Callable:
        executor_name = name or id.replace("_", " ").title()

        cls = _build_executor_class(
            executor_id=id,
            executor_name=executor_name,
            executor_description=description,
            fn=fn,
            inputs_schema=resolved_input,
            outputs_schema=resolved_output,
            tags=tags or [],
        )

        # Register in extension registry
        executor_register(override=override)(cls)

        # Store for bridge discovery
        _function_executor_classes[id] = cls

        logger.info(f"Registered function executor: {id}")
        return fn

    return decorator


def _resolve_schema(
    explicit_schema: Optional[Dict[str, Any]],
    pydantic_model: Optional[Type[BaseModel]],
) -> Optional[Union[Type[BaseModel], Dict[str, Any]]]:
    """Resolve schema from explicit dict or Pydantic model.

    Raises ValueError if both are provided.
    """
    if explicit_schema is not None and pydantic_model is not None:
        raise ValueError(
            "input_schema/output_schema and input_model/output_model are mutually exclusive"
        )
    if pydantic_model is not None:
        return pydantic_model
    return explicit_schema


def _build_executor_class(
    executor_id: str,
    executor_name: str,
    executor_description: str,
    fn: Callable,
    inputs_schema: Optional[Union[Type[BaseModel], Dict[str, Any]]],
    outputs_schema: Optional[Union[Type[BaseModel], Dict[str, Any]]],
    tags: list[str],
) -> type:
    """Create a BaseTask subclass dynamically from a function."""
    is_async = inspect.iscoroutinefunction(fn)

    if is_async:

        async def execute(self: Any, inputs: Dict[str, Any]) -> Dict[str, Any]:
            return await fn(inputs)

    else:

        async def execute(self: Any, inputs: Dict[str, Any]) -> Dict[str, Any]:
            return await asyncio.to_thread(fn, inputs)

    # Build class name: "analyze_pr" → "AnalyzePrFunctionExecutor"
    class_name = "".join(part.capitalize() for part in executor_id.split("_")) + "FunctionExecutor"

    attrs: Dict[str, Any] = {
        "id": executor_id,
        "name": executor_name,
        "description": executor_description,
        "tags": tags,
        "execute": execute,
    }

    if inputs_schema is not None:
        attrs["inputs_schema"] = inputs_schema
    if outputs_schema is not None:
        attrs["outputs_schema"] = outputs_schema

    cls = type(class_name, (BaseTask,), attrs)
    return cls


def get_function_executor_classes() -> Dict[str, type]:
    """Return a copy of the function executor registry.

    Used by the bridge to discover runtime-registered function executors
    that are invisible to the AST scanner.
    """
    return dict(_function_executor_classes)
