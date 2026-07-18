"""
Executor metadata utilities for demo task generation and validation

This module provides utilities to query executor metadata and validate
demo task definitions against executor schemas.
"""

from typing import Dict, Any, Optional
from apflow.core.extensions.registry import get_registry
from apflow.core.extensions.types import ExtensionCategory
from apflow.logger import get_logger
from apflow.core.utils.helpers import validate_input_schema as _validate_input_schema

logger = get_logger(__name__)


def get_executor_metadata(executor_id: str) -> Optional[Dict[str, Any]]:
    """
    Get executor metadata for demo task generation

    Returns comprehensive metadata about an executor including:
    - id, name, description
    - input_schema (for validation)
    - examples (text descriptions)
    - tags

    Args:
        executor_id: Executor ID (e.g., "system_info_executor")

    Returns:
        Dictionary with executor metadata, or None if executor not found

    Example:
        metadata = get_executor_metadata("system_info_executor")
        # Returns:
        # {
        #     "id": "system_info_executor",
        #     "name": "System Info Executor",
        #     "description": "Query system resource information",
        #     "input_schema": {...},
        #     "examples": ["Get CPU information", ...],
        #     "tags": ["stdio", "system", "info"]
        # }
    """
    registry = get_registry()
    extension = registry.get_by_id(executor_id)

    if not extension:
        logger.warning(f"Executor '{executor_id}' not found in registry")
        return None

    if extension.category != ExtensionCategory.EXECUTOR:
        logger.warning(
            f"Extension '{executor_id}' is not an executor (category: {extension.category.value})"
        )
        return None

    # Create executor instance to get metadata
    # Use empty inputs for metadata query
    try:
        executor_instance = registry.create_executor_instance(executor_id, inputs={})
    except Exception as e:
        logger.warning(f"Failed to create executor instance for '{executor_id}': {e}")
        # Fallback: return basic metadata from extension. Use getattr with
        # defaults throughout — an extension lacking one of these attributes
        # must degrade to a best-effort dict, not raise (see docstring's
        # documented None-only failure contract).
        return {
            "id": getattr(extension, "id", executor_id),
            "name": getattr(extension, "name", executor_id),
            "description": getattr(extension, "description", "") or "",
            "input_schema": {},
            "examples": getattr(extension, "examples", []) or [],
            "tags": getattr(extension, "tags", []) or [],
        }

    # Get input schema from executor instance
    input_schema = {}
    if hasattr(executor_instance, "get_input_schema"):
        try:
            input_schema = executor_instance.get_input_schema()
        except Exception as e:
            logger.warning(f"Failed to get input schema from executor '{executor_id}': {e}")

    # Collect metadata
    metadata = {
        "id": getattr(extension, "id", executor_id),
        "name": getattr(extension, "name", executor_id),
        "description": getattr(extension, "description", "") or "",
        "input_schema": input_schema,
        "examples": getattr(extension, "examples", []) or [],
        "tags": getattr(extension, "tags", []) or [],
    }

    # Add type if available
    if hasattr(extension, "type"):
        metadata["type"] = extension.type

    return metadata


def validate_task_format(task: Dict[str, Any], executor_id: str) -> bool:
    """
    Validate task against executor's input schema

    Checks if the task's inputs match the executor's expected input schema.
    This ensures tasks are accurate and will work correctly when executed.

    Args:
        task: Task dictionary (must have "inputs" and "schemas.method" fields)
        executor_id: Executor ID to validate against (optional, can be extracted from task)

    Returns:
        True if task format is valid, False otherwise

    Example:
        task = {
            "name": "CPU Analysis",
            "schemas": {"method": "system_info_executor"},
            "inputs": {"resource": "cpu"}
        }
        is_valid = validate_task_format(task, "system_info_executor")
    """
    # Extract executor_id from task if not provided
    if not executor_id:
        schemas = task.get("schemas", {})
        executor_id = schemas.get("method")
        if not executor_id:
            logger.warning(
                "Cannot validate task: no executor_id provided and task.schemas.method is missing"
            )
            return False

    # Get executor metadata
    metadata = get_executor_metadata(executor_id)
    if not metadata:
        logger.warning(f"Cannot validate task: executor '{executor_id}' not found")
        return False

    # Get input schema
    input_schema = metadata.get("input_schema", {})
    if not input_schema:
        # No schema defined, assume valid
        logger.debug(f"Executor '{executor_id}' has no input schema, skipping validation")
        return True

    # Get task inputs
    task_inputs = task.get("inputs", {})
    if not task_inputs:
        # Check if inputs are required
        required_fields = input_schema.get("required", [])
        if required_fields:
            logger.warning(
                f"Task has no inputs but executor '{executor_id}' requires: {required_fields}"
            )
            return False
        # No inputs and no required fields, valid
        return True

    # Validate inputs against schema
    try:
        is_valid = _validate_input_schema(input_schema, task_inputs)
        if not is_valid:
            logger.warning(
                f"Task inputs do not match executor '{executor_id}' schema. "
                f"Task inputs: {task_inputs}, Schema: {input_schema}"
            )
        return is_valid
    except Exception as e:
        logger.warning(f"Error validating task inputs against executor '{executor_id}' schema: {e}")
        return False


def get_all_executor_metadata() -> Dict[str, Dict[str, Any]]:
    """
    Get metadata for all registered executors

    Returns:
        Dictionary mapping executor_id -> metadata dict

    Example:
        all_metadata = get_all_executor_metadata()
        # Returns:
        # {
        #     "system_info_executor": {...},
        #     "command_executor": {...},
        #     ...
        # }
    """
    registry = get_registry()
    all_metadata = {}

    # Get all executor extensions
    executors = registry.list_executors()

    for executor in executors:
        executor_id = executor.id
        try:
            metadata = get_executor_metadata(executor_id)
        except Exception as e:
            # One bad executor must not abort metadata collection for every
            # other registered executor.
            logger.warning(f"Skipping executor '{executor_id}' in metadata collection: {e}")
            continue
        if metadata:
            all_metadata[executor_id] = metadata

    return all_metadata
