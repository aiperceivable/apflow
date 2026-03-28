"""
Create and populate an apcore Registry with all apflow modules.

This is the main entry point for apcore integration. Call create_apflow_registry()
to get a fully populated Registry that can be passed to apcore-mcp, apcore-a2a,
or apcore-cli.

Example:
    from apflow.bridge import create_apflow_registry

    registry = create_apflow_registry(task_manager, task_creator, task_repository)

    # Expose via MCP
    from apcore_mcp import serve_mcp
    serve_mcp(registry, transport="streamable-http")
"""

from typing import Any

from apcore import APCore, Registry

from apflow.bridge.scanner_bridge import discover_executor_modules
from apflow.bridge.task_modules import (
    TaskCreateModule,
    TaskDeleteModule,
    TaskExecuteModule,
    TaskGetModule,
    TaskListModule,
)
from apflow.logger import get_logger

logger = get_logger(__name__)


def create_apflow_registry(
    task_manager: Any,
    task_creator: Any,
    task_repository: Any,
    namespace: str = "apflow",
) -> Registry:
    """Create and populate an apcore Registry with all apflow modules.

    Args:
        task_manager: TaskManager instance for task execution.
        task_creator: TaskCreator instance for task creation.
        task_repository: TaskRepository instance for task CRUD.
        namespace: Module ID prefix (default: "apflow").

    Returns:
        Populated apcore Registry.

    Raises:
        RuntimeError: If APCore initialization fails.
        ValueError: If namespace is empty.
    """
    if not namespace:
        raise ValueError("namespace must be non-empty")

    try:
        client = APCore()
    except Exception as e:
        raise RuntimeError(f"Failed to initialize APCore: {e}") from e

    registry = client.registry

    # 1. Register discovered executor modules
    executor_adapters = discover_executor_modules()
    for adapter in executor_adapters:
        module_id = f"{namespace}.{adapter._executor_id}"
        try:
            client.register(module_id, adapter)
            logger.debug(f"Registered executor module: {module_id}")
        except Exception as e:
            logger.warning(f"Failed to register {module_id}: {e}")

    # 2. Register task management modules
    task_modules = {
        "task.create": TaskCreateModule(task_creator, task_repository),
        "task.execute": TaskExecuteModule(task_manager),
        "task.list": TaskListModule(task_repository),
        "task.get": TaskGetModule(task_repository),
        "task.delete": TaskDeleteModule(task_repository),
    }
    for action, module in task_modules.items():
        module_id = f"{namespace}.{action}"
        try:
            client.register(module_id, module)
            logger.debug(f"Registered task module: {module_id}")
        except Exception as e:
            logger.warning(f"Failed to register {module_id}: {e}")

    total = len(list(registry.list()))
    logger.info(f"apcore Registry populated with {total} modules (namespace: {namespace})")

    return registry
