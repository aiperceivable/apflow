"""
Create and populate an apcore Registry with all apflow modules.

This is the main entry point for apcore integration. Call create_apflow_registry()
to get a fully populated Registry that can be passed to apcore-mcp, apcore-a2a,
or apcore-cli.

Example:
    from apflow.bridge import create_apflow_registry

    registry = create_apflow_registry(task_manager, task_creator, task_repository)

    # Expose via MCP
    from apcore_mcp import serve
    serve(registry, transport="streamable-http")
"""

from typing import Any

from apcore import APCore, Registry

from apflow.bridge.scanner_bridge import discover_executor_modules
from apflow.bridge.governance_modules import BudgetScopeModule, BudgetReconcileModule
from apflow.bridge.execution_modules import TaskSubmitModule, TaskReceiptModule, TaskReconcileModule
from apflow.bridge.schedule_modules import (
    ScheduleCompleteModule,
    ScheduleDueModule,
    ScheduleExportICalModule,
    ScheduleHistoryModule,
    ScheduleSetModule,
    ScheduleTriggerModule,
)
from apflow.bridge.task_modules import (
    TaskCreateModule,
    TaskDeleteModule,
    TaskExecuteModule,
    TaskArchiveModule,
    TaskCancelModule,
    TaskChildrenModule,
    TaskCloneMixedModule,
    TaskCopyModule,
    TaskCreateTreeModule,
    TaskGetModule,
    TaskLinkModule,
    TaskListModule,
    TaskRunningListModule,
    TaskScheduledListModule,
    TaskTreeModule,
    TaskUpdateModule,
)
from apflow.logger import get_logger

logger = get_logger(__name__)


def validate_required_modules(
    registry: Registry,
    required_modules: list[str] | None = None,
    required_schema_versions: dict[str, str] | None = None,
) -> None:
    """Refuse readiness when a required capability is missing or degraded."""
    versions = required_schema_versions or {}
    required = set(required_modules or []) | set(versions)
    for module_id in sorted(required):
        if not registry.has(module_id):
            raise RuntimeError(f"Required module is unavailable: {module_id}")
        definition = registry.get_definition(module_id)
        metadata = getattr(definition, "metadata", {}) or {}
        if metadata.get("schema_fallback"):
            raise RuntimeError(f"Required module has a fallback schema: {module_id}")
        if module_id in versions and metadata.get("schema_version") != versions[module_id]:
            raise RuntimeError(f"Required module schema version mismatch: {module_id}")


def create_apflow_registry(
    task_manager: Any,
    task_creator: Any,
    task_repository: Any,
    namespace: str = "apflow",
    *,
    required_modules: list[str] | None = None,
    required_schema_versions: dict[str, str] | None = None,
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
        RuntimeError: If APCore initialization fails, or if a built-in
            task/schedule module fails to register (e.g. a discovered
            executor's id collides with the reserved namespace).
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
        module_id = f"{namespace}.{adapter.executor_id}"
        adapter.module_id = module_id
        try:
            client.register(module_id, adapter)
            logger.debug(f"Registered executor module: {module_id}")
        except Exception as e:
            logger.warning(f"Failed to register {module_id}: {e}")

    # 2. Register task management modules
    task_modules = {
        "budget.scope": BudgetScopeModule(task_repository),
        "budget.reconcile": BudgetReconcileModule(task_repository),
        "task.create": TaskCreateModule(task_creator, task_repository),
        "task.create_tree": TaskCreateTreeModule(task_creator, task_repository),
        "task.execute": TaskExecuteModule(task_manager),
        "task.submit": TaskSubmitModule(task_repository),
        "task.receipt": TaskReceiptModule(task_repository),
        "task.reconcile": TaskReconcileModule(task_repository),
        "task.cancel": TaskCancelModule(task_manager),
        "task.get": TaskGetModule(task_repository),
        "task.update": TaskUpdateModule(task_repository),
        "task.list": TaskListModule(task_repository),
        "task.delete": TaskDeleteModule(task_repository),
        "task.tree": TaskTreeModule(task_repository),
        "task.children": TaskChildrenModule(task_repository),
        "task.link": TaskLinkModule(task_creator, task_repository),
        "task.copy": TaskCopyModule(task_creator, task_repository),
        "task.archive": TaskArchiveModule(task_creator, task_repository),
        "task.clone_mixed": TaskCloneMixedModule(task_creator, task_repository),
        "task.running": TaskRunningListModule(task_repository),
        "task.scheduled": TaskScheduledListModule(task_repository),
        "schedule.set": ScheduleSetModule(task_repository),
        "schedule.due": ScheduleDueModule(task_repository),
        "schedule.trigger": ScheduleTriggerModule(task_repository),
        "schedule.complete": ScheduleCompleteModule(task_repository),
        "schedule.history": ScheduleHistoryModule(task_repository),
        "schedule.export_ical": ScheduleExportICalModule(task_repository),
    }
    for action, module in task_modules.items():
        module_id = f"{namespace}.{action}"
        setattr(module, "module_id", module_id)
        try:
            client.register(module_id, module)
            logger.debug(f"Registered task module: {module_id}")
        except Exception as e:
            # These are apflow's own built-in capabilities, not optional
            # extensions like the discovered executor modules above — a
            # failure here (most commonly a discovered executor's id
            # colliding with this reserved task.*/schedule.* namespace) must
            # not be silently swallowed, or the built-in capability just
            # disappears from the registry with no signal to the operator.
            raise RuntimeError(f"Failed to register built-in module '{module_id}': {e}") from e

    validate_required_modules(registry, required_modules, required_schema_versions)
    total = len(list(registry.list()))
    logger.info(f"apcore Registry populated with {total} modules (namespace: {namespace})")

    return registry
