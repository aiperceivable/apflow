"""
Global configuration registry for apflow

This module provides a centralized registry for managing global configuration
like task model class and hooks. Components can access configuration without
passing parameters through multiple layers.
"""

import importlib
import os
from threading import local
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from apflow.core.types import TaskPreHook, TaskPostHook, WebhookVerifyHook
from apflow.logger import get_logger

if TYPE_CHECKING:
    from apflow.core.distributed.config import DistributedConfig
    from apflow.core.storage.sqlalchemy.models import TaskModel, TaskModelType

logger = get_logger(__name__)

# Thread-local storage for configuration (supports multi-threaded scenarios)
_thread_local = local()


class ConfigRegistry:
    """
    Global configuration registry

    This class manages global configuration like:
    - Task model class
    - Pre-execution hooks
    - Post-execution hooks

    Uses thread-local storage to support multi-threaded scenarios.
    """

    def __init__(self):
        """Initialize empty registry"""
        self._task_model_class: Optional["TaskModelType"] = None
        self._pre_hooks: List[TaskPreHook] = []
        self._post_hooks: List[TaskPostHook] = []
        self._use_task_creator: Optional[type] = None  # Default to True for rigorous task creation
        self._require_existing_tasks: bool = False  # Default to False for convenience (auto-create)
        # Execution components (F-003 durability, F-004 governance). Process-singleton,
        # session-free components (PolicyEngine, CircuitBreakerRegistry) are stored here so
        # TaskExecutor can hand them to every per-execution TaskManager. Session-bound
        # components (BudgetManager, CheckpointManager) are NOT stored — they are built
        # per-execution from the live session; these flags say whether to build them.
        self._policy_engine: Optional[Any] = None
        self._circuit_breaker_registry: Optional[Any] = None
        self._governance_enabled: bool = False
        self._durability_enabled: bool = False
        # Task tree lifecycle hooks
        self._task_tree_hooks: Dict[str, List[Callable]] = {
            "on_tree_created": [],
            "on_tree_started": [],
            "on_tree_completed": [],
            "on_tree_failed": [],
        }
        # Webhook verify hook for custom authentication
        self._webhook_verify_hook: Optional[WebhookVerifyHook] = None
        # Demo mode sleep scale factor - multiplies executor-specific _demo_sleep values
        # Default: read from environment variable APFLOW_DEMO_SLEEP_SCALE, or 1.0 (no scaling)
        # Example: executor returns _demo_sleep=2.0, global scale=0.5 → actual sleep=1.0s
        self._demo_sleep_scale: float = float(os.getenv("APFLOW_DEMO_SLEEP_SCALE", "1.0"))
        # Distributed config (lazy-loaded)
        self._distributed_config: Optional["DistributedConfig"] = None

    def set_task_model_class(self, task_model_class: Optional["TaskModel"]) -> None:
        """
        Set the global task model class

        Args:
            task_model_class: Custom TaskModel class, or None for default

        Raises:
            TypeError: If task_model_class is not a subclass of TaskModel
        """
        if task_model_class:
            # Import TaskModel dynamically for comparison
            from apflow.core.storage.sqlalchemy.models import (
                TaskModel as ImportedTaskModel,
            )

            # Check if task_model_class is a subclass of TaskModel
            # Use MRO (Method Resolution Order) to handle cases where TaskModel might have been reloaded
            is_subclass = False
            try:
                is_subclass = issubclass(task_model_class, ImportedTaskModel)
            except TypeError:
                # If issubclass fails (e.g., due to module reload), check MRO
                is_subclass = False

            # If issubclass failed or returned False, check MRO for any TaskModel class
            # This handles cases where TaskModel was reloaded and CustomTaskModel inherits from old TaskModel
            if not is_subclass and hasattr(task_model_class, "__mro__"):
                for base in task_model_class.__mro__:
                    # Check if base is TaskModel (current) or has TaskModel name and correct module
                    if base is ImportedTaskModel or (
                        hasattr(base, "__name__")
                        and base.__name__ == "TaskModel"
                        and hasattr(base, "__module__")
                        and "apflow.core.storage.sqlalchemy.models" in base.__module__
                    ):
                        is_subclass = True
                        break

            if not is_subclass:
                raise TypeError(
                    f"task_model_class must be a subclass of TaskModel, "
                    f"got {task_model_class}. "
                    f"Please ensure your custom class inherits from TaskModel:\n"
                    f"  from apflow.core.storage.sqlalchemy.models import TaskModel\n"
                    f"  class MyTaskModel(TaskModel):\n"
                    f"      # Your custom fields here\n"
                    f"      pass"
                )
        self._task_model_class = task_model_class
        logger.debug(
            f"Set global task model class: "
            f"{task_model_class.__name__ if task_model_class else 'TaskModel'}"
        )

    def get_task_model_class(self) -> "TaskModel":
        """
        Get the global task model class

        Returns:
            TaskModel class (default or custom)
        """
        if self._task_model_class:
            return self._task_model_class

        # Lazy import TaskModel on first access
        from apflow.core.storage.sqlalchemy.models import TaskModel

        return TaskModel

    def register_pre_hook(self, hook: TaskPreHook) -> None:
        """
        Register a pre-execution hook

        Args:
            hook: Pre-execution hook function (sync or async)
        """
        if hook not in self._pre_hooks:
            self._pre_hooks.append(hook)
            logger.debug(
                f"Registered pre-hook: {hook.__name__ if hasattr(hook, '__name__') else str(hook)}"
            )

    def register_post_hook(self, hook: TaskPostHook) -> None:
        """
        Register a post-execution hook

        Args:
            hook: Post-execution hook function (sync or async)
        """
        if hook not in self._post_hooks:
            self._post_hooks.append(hook)
            logger.debug(
                f"Registered post-hook: {hook.__name__ if hasattr(hook, '__name__') else str(hook)}"
            )

    def get_pre_hooks(self) -> List[TaskPreHook]:
        """
        Get all registered pre-execution hooks

        Returns:
            List of pre-hook functions
        """
        return self._pre_hooks.copy()

    def get_post_hooks(self) -> List[TaskPostHook]:
        """
        Get all registered post-execution hooks

        Returns:
            List of post-hook functions
        """
        return self._post_hooks.copy()

    def set_use_task_creator(self, use_task_creator: Optional[type]) -> None:
        """
        Set whether to use TaskCreator for rigorous task creation

        Args:
            use_task_creator: If use_task_creator, use TaskCreator.create_task_tree_from_array for rigorous validation.
                             If not, use quick create mode (not recommended, may have issues).
                             Default is TaskCreator for rigorous creation.
        """
        self._use_task_creator = use_task_creator
        logger.debug(f"Set use_task_creator: {use_task_creator}")

    def get_use_task_creator(self) -> type:
        """
        Get whether to use TaskCreator for task creation

        Returns:
            True if TaskCreator should be used (default), False otherwise
        """
        if self._use_task_creator is not None:
            return self._use_task_creator
        # Use importlib to avoid circular import with task_creator module
        module = importlib.import_module("apflow.core.execution.task_creator")
        return module.TaskCreator

    def set_require_existing_tasks(self, require_existing_tasks: bool) -> None:
        """
        Set whether to require tasks to exist before execution

        Args:
            require_existing_tasks: If True, only execute tasks that already exist in database.
                                  If False (default), create tasks if they don't exist (more convenient).
                                  Default is False for convenience (auto-create).
        """
        self._require_existing_tasks = require_existing_tasks
        logger.debug(f"Set require_existing_tasks: {require_existing_tasks}")

    def get_require_existing_tasks(self) -> bool:
        """
        Get whether to require tasks to exist before execution

        Returns:
            True if only existing tasks should be executed, False if tasks can be auto-created (default)
        """
        return self._require_existing_tasks

    # --- Execution components: durability (F-003) + governance (F-004) ---

    def set_policy_engine(self, policy_engine: Optional[Any]) -> None:
        """Register the process-wide PolicyEngine (cost-policy evaluation)."""
        self._policy_engine = policy_engine

    def get_policy_engine(self) -> Optional[Any]:
        """Get the registered PolicyEngine, or None."""
        return self._policy_engine

    def set_circuit_breaker_registry(self, registry: Optional[Any]) -> None:
        """Register the process-wide CircuitBreakerRegistry (shared trip state)."""
        self._circuit_breaker_registry = registry

    def get_circuit_breaker_registry(self) -> Optional[Any]:
        """Get the registered CircuitBreakerRegistry, or None."""
        return self._circuit_breaker_registry

    def set_governance_enabled(self, enabled: bool) -> None:
        """Enable per-execution budget enforcement (BudgetManager built per session)."""
        self._governance_enabled = enabled

    def is_governance_enabled(self) -> bool:
        """Whether the executor should build a BudgetManager for each execution."""
        return self._governance_enabled

    def set_durability_enabled(self, enabled: bool) -> None:
        """Enable per-execution durability (CheckpointManager/RetryManager per session)."""
        self._durability_enabled = enabled

    def is_durability_enabled(self) -> bool:
        """Whether the executor should build checkpoint/retry managers for each execution."""
        return self._durability_enabled

    def register_task_tree_hook(self, hook_type: str, hook: Callable) -> None:
        """
        Register a task tree lifecycle hook

        Args:
            hook_type: One of "on_tree_created", "on_tree_started",
                      "on_tree_completed", "on_tree_failed"
            hook: Hook function (sync or async)
                 Signature: async def hook(root_task: TaskModel, *args) -> None
        """
        if hook_type not in self._task_tree_hooks:
            raise ValueError(
                f"Invalid hook_type: {hook_type}. "
                f"Must be one of: {list(self._task_tree_hooks.keys())}"
            )
        if hook not in self._task_tree_hooks[hook_type]:
            self._task_tree_hooks[hook_type].append(hook)
            logger.debug(
                f"Registered task tree hook '{hook_type}': "
                f"{hook.__name__ if hasattr(hook, '__name__') else str(hook)}"
            )

    def get_task_tree_hooks(self, hook_type: str) -> List[Callable]:
        """
        Get all registered task tree hooks for a specific hook type

        Args:
            hook_type: One of "on_tree_created", "on_tree_started",
                      "on_tree_completed", "on_tree_failed"

        Returns:
            List of hook functions
        """
        return self._task_tree_hooks.get(hook_type, []).copy()

    def set_demo_sleep_scale(self, scale: float) -> None:
        """
        Set demo mode sleep scale factor

        This multiplies executor-specific _demo_sleep values to adjust demo execution time.
        Each executor can define its own sleep time via _demo_sleep in get_demo_result(),
        and this global scale factor is applied to all of them.

        Examples:
            - scale=1.0: No scaling (use executor's _demo_sleep as-is)
            - scale=0.5: Half the sleep time (faster for testing)
            - scale=2.0: Double the sleep time (slower for realistic simulation)
            - scale=0.0: No sleep at all (fastest, ignores executor _demo_sleep)

        Args:
            scale: Scale factor (default: 1.0, or from APFLOW_DEMO_SLEEP_SCALE env var)
        """
        self._demo_sleep_scale = float(scale)
        logger.debug(f"Set demo_sleep_scale: {scale}")

    def get_demo_sleep_scale(self) -> float:
        """
        Get demo mode sleep scale factor

        Returns:
            Scale factor (default: 1.0, or from APFLOW_DEMO_SLEEP_SCALE env var)
        """
        return self._demo_sleep_scale

    def register_webhook_verify_hook(self, hook: WebhookVerifyHook) -> None:
        """
        Register a webhook verify hook for custom authentication

        Only one hook is allowed since verification logic should come from a single source.

        Args:
            hook: Webhook verify hook function (sync or async)
        """
        self._webhook_verify_hook = hook
        logger.debug(
            f"Registered webhook verify hook: "
            f"{hook.__name__ if hasattr(hook, '__name__') else str(hook)}"
        )

    def get_webhook_verify_hook(self) -> Optional[WebhookVerifyHook]:
        """
        Get the registered webhook verify hook

        Returns:
            Webhook verify hook function, or None if not registered
        """
        return self._webhook_verify_hook

    def set_distributed_config(self, config: "DistributedConfig") -> None:
        """Set the distributed cluster configuration."""
        self._distributed_config = config
        logger.debug(
            f"Set distributed config: enabled={config.enabled}, " f"node_id={config.node_id}"
        )

    def get_distributed_config(self) -> "DistributedConfig":
        """Get the distributed cluster configuration.

        Returns a disabled DistributedConfig if not explicitly set.
        """
        if self._distributed_config is not None:
            return self._distributed_config
        from apflow.core.distributed.config import DistributedConfig

        return DistributedConfig()

    def clear(self) -> None:
        """Clear all configuration (useful for testing)"""
        self._distributed_config = None
        self._task_model_class = None
        self._pre_hooks.clear()
        self._post_hooks.clear()
        self._use_task_creator = None  # Reset to default
        self._require_existing_tasks = False  # Reset to default
        self._webhook_verify_hook = None
        # Reset execution components (durability/governance) to default (disabled)
        self._policy_engine = None
        self._circuit_breaker_registry = None
        self._governance_enabled = False
        self._durability_enabled = False
        # Clear task tree hooks
        for hook_list in self._task_tree_hooks.values():
            hook_list.clear()
        # Reset TaskExecutor singleton to avoid test pollution
        # Use importlib to avoid circular import with task_executor module
        try:
            module = importlib.import_module("apflow.core.execution.task_executor")
            module.TaskExecutor._instance = None
            module.TaskExecutor._initialized = False
        except ImportError:
            pass
        logger.debug("Cleared configuration registry and reset TaskExecutor singleton")


# Global registry instance (singleton pattern)
_global_registry = ConfigRegistry()


def _get_registry() -> ConfigRegistry:
    """
    Get the current configuration registry

    Uses a singleton pattern with global registry.
    Can be extended to support thread-local storage for multi-threaded scenarios.

    Returns:
        ConfigRegistry instance (global singleton)
    """
    return _global_registry


def get_config() -> ConfigRegistry:
    """
    Get the current configuration registry

    Returns:
        ConfigRegistry instance
    """
    return _get_registry()


def set_task_model_class(task_model_class: Optional["TaskModel"]) -> None:
    """
    Set the global task model class

    Args:
        task_model_class: Custom TaskModel class, or None for default
    """
    _get_registry().set_task_model_class(task_model_class)


def get_task_model_class() -> "TaskModel":
    """
    Get the global task model class

    Returns:
        TaskModel class (default or custom)
    """
    return _get_registry().get_task_model_class()


def register_pre_hook(hook: Optional[TaskPreHook] = None) -> Callable:
    """
    Register a pre-execution hook using decorator syntax

    Can be used as a decorator:
        @register_pre_hook
        async def my_pre_hook(task):
            ...

    Or called directly:
        register_pre_hook(my_pre_hook)

    Args:
        hook: Pre-execution hook function (sync or async), or None when used as decorator

    Returns:
        Hook function (for decorator usage) or None (for direct call)
    """

    def decorator(func: TaskPreHook) -> TaskPreHook:
        _get_registry().register_pre_hook(func)
        return func

    if hook is None:
        # Used as decorator: @register_pre_hook
        return decorator
    else:
        # Used as function call: register_pre_hook(my_hook)
        _get_registry().register_pre_hook(hook)
        return hook


def register_post_hook(hook: Optional[TaskPostHook] = None) -> Callable:
    """
    Register a post-execution hook using decorator syntax

    Can be used as a decorator:
        @register_post_hook
        async def my_post_hook(task, inputs, result):
            ...

    Or called directly:
        register_post_hook(my_post_hook)

    Args:
        hook: Post-execution hook function (sync or async), or None when used as decorator

    Returns:
        Hook function (for decorator usage) or None (for direct call)
    """

    def decorator(func: TaskPostHook) -> TaskPostHook:
        _get_registry().register_post_hook(func)
        return func

    if hook is None:
        # Used as decorator: @register_post_hook
        return decorator
    else:
        # Used as function call: register_post_hook(my_hook)
        _get_registry().register_post_hook(hook)
        return hook


def get_pre_hooks() -> List[TaskPreHook]:
    """
    Get all registered pre-execution hooks

    Returns:
        List of pre-hook functions
    """
    return _get_registry().get_pre_hooks()


def get_post_hooks() -> List[TaskPostHook]:
    """
    Get all registered post-execution hooks

    Returns:
        List of post-hook functions
    """
    return _get_registry().get_post_hooks()


def clear_config() -> None:
    """Clear all configuration (useful for testing)"""
    _get_registry().clear()


def set_use_task_creator(use_task_creator: bool) -> None:
    """
    Set whether to use TaskCreator for rigorous task creation

    Args:
        use_task_creator: If True, use TaskCreator.create_task_tree_from_array for rigorous validation.
                         If False, use quick create mode (not recommended, may have issues).
                         Default is True.
    """
    _get_registry().set_use_task_creator(use_task_creator)


def get_use_task_creator() -> type:
    """
    Get whether to use TaskCreator for task creation

    Returns:
        True if TaskCreator should be used (default), False otherwise
    """
    return _get_registry().get_use_task_creator()


def set_require_existing_tasks(require_existing_tasks: bool) -> None:
    """
    Set whether to require tasks to exist before execution

    Args:
        require_existing_tasks: If True, only execute tasks that already exist in database.
                              If False (default), create tasks if they don't exist (more convenient).
                              Default is False for convenience (auto-create).
    """
    _get_registry().set_require_existing_tasks(require_existing_tasks)


# --- Execution components: durability (F-003) + governance (F-004) ---


def set_policy_engine(policy_engine: Optional[Any]) -> None:
    """Register the process-wide PolicyEngine (cost-policy evaluation)."""
    _get_registry().set_policy_engine(policy_engine)


def get_policy_engine() -> Optional[Any]:
    """Get the registered PolicyEngine, or None."""
    return _get_registry().get_policy_engine()


def set_circuit_breaker_registry(registry: Optional[Any]) -> None:
    """Register the process-wide CircuitBreakerRegistry (shared trip state)."""
    _get_registry().set_circuit_breaker_registry(registry)


def get_circuit_breaker_registry() -> Optional[Any]:
    """Get the registered CircuitBreakerRegistry, or None."""
    return _get_registry().get_circuit_breaker_registry()


def set_governance_enabled(enabled: bool) -> None:
    """Enable per-execution budget enforcement (BudgetManager built per session)."""
    _get_registry().set_governance_enabled(enabled)


def is_governance_enabled() -> bool:
    """Whether the executor should build a BudgetManager for each execution."""
    return _get_registry().is_governance_enabled()


def set_durability_enabled(enabled: bool) -> None:
    """Enable per-execution durability (CheckpointManager/RetryManager per session)."""
    _get_registry().set_durability_enabled(enabled)


def is_durability_enabled() -> bool:
    """Whether the executor should build checkpoint/retry managers for each execution."""
    return _get_registry().is_durability_enabled()


def set_demo_sleep_scale(scale: float) -> None:
    """
    Set demo mode sleep scale factor

    This multiplies executor-specific _demo_sleep values to adjust demo execution time.
    Each executor can define its own sleep time via _demo_sleep in get_demo_result(),
    and this global scale factor is applied to all of them.

    Examples:
        - scale=1.0: No scaling (use executor's _demo_sleep as-is)
        - scale=0.5: Half the sleep time (faster for testing)
        - scale=2.0: Double the sleep time (slower for realistic simulation)
        - scale=0.0: No sleep at all (fastest, ignores executor _demo_sleep)

    Args:
        scale: Scale factor (default: 1.0, or from APFLOW_DEMO_SLEEP_SCALE env var)

    Example:
        from apflow.core.config import set_demo_sleep_scale
        set_demo_sleep_scale(0.5)  # Reduce all executor sleep times by half
    """
    _get_registry().set_demo_sleep_scale(scale)


def get_demo_sleep_scale() -> float:
    """
    Get demo mode sleep scale factor

    Returns:
        Scale factor (default: 1.0, or from APFLOW_DEMO_SLEEP_SCALE env var)

    Example:
        from apflow.core.config import get_demo_sleep_scale
        scale = get_demo_sleep_scale()  # Get current demo sleep scale
    """
    return _get_registry().get_demo_sleep_scale()


def get_require_existing_tasks() -> bool:
    """
    Get whether to require tasks to exist before execution

    Returns:
        True if only existing tasks should be executed, False if tasks can be auto-created (default)
    """
    return _get_registry().get_require_existing_tasks()


def register_webhook_verify_hook(
    hook: Optional[WebhookVerifyHook] = None,
) -> Callable:
    """
    Register a webhook verify hook using decorator syntax

    Can be used as a decorator:
        @register_webhook_verify_hook
        async def my_verify(ctx):
            ...

    Or called directly:
        register_webhook_verify_hook(my_verify)

    Args:
        hook: Webhook verify hook function (sync or async), or None when used as decorator

    Returns:
        Hook function (for decorator usage)
    """

    def decorator(func: WebhookVerifyHook) -> WebhookVerifyHook:
        _get_registry().register_webhook_verify_hook(func)
        return func

    if hook is None:
        return decorator
    else:
        _get_registry().register_webhook_verify_hook(hook)
        return hook


def get_webhook_verify_hook() -> Optional[WebhookVerifyHook]:
    """
    Get the registered webhook verify hook

    Returns:
        Webhook verify hook function, or None if not registered
    """
    return _get_registry().get_webhook_verify_hook()


def set_distributed_config(config: "DistributedConfig") -> None:
    """Set the distributed cluster configuration."""
    _get_registry().set_distributed_config(config)


def get_distributed_config() -> "DistributedConfig":
    """Get the distributed cluster configuration.

    Returns a disabled DistributedConfig if not explicitly set.
    """
    return _get_registry().get_distributed_config()


def register_task_tree_hook(hook_type: Optional[str] = None):
    """
    Register a task tree lifecycle hook using decorator syntax

    Can be used as a decorator:
        @register_task_tree_hook("on_tree_completed")
        async def on_tree_completed(root_task, status):
            ...

    Or called directly:
        register_task_tree_hook("on_tree_completed")(my_hook)

    Args:
        hook_type: One of "on_tree_created", "on_tree_started",
                  "on_tree_completed", "on_tree_failed"

    Returns:
        Decorator function or None
    """

    def decorator(func: Callable) -> Callable:
        if hook_type is None:
            raise ValueError("hook_type must be provided when using as decorator")
        _get_registry().register_task_tree_hook(hook_type, func)
        return func

    if hook_type is None:
        # Used as decorator without parentheses: @register_task_tree_hook
        # This is not supported - hook_type is required
        raise ValueError(
            "hook_type is required. Use: @register_task_tree_hook('on_tree_completed')"
        )
    else:
        # Used as decorator with parentheses: @register_task_tree_hook("on_tree_completed")
        return decorator


def get_task_tree_hooks(hook_type: str) -> List[Callable]:
    """
    Get all registered task tree hooks for a specific hook type

    Args:
        hook_type: One of "on_tree_created", "on_tree_started",
                  "on_tree_completed", "on_tree_failed"

    Returns:
        List of hook functions
    """
    return _get_registry().get_task_tree_hooks(hook_type)


def task_model_register():
    """
    Decorator to register a custom TaskModel class

    Can be used as a decorator:
        @task_model_register()
        class MyTaskModel(TaskModel):
            ...

    This automatically calls set_task_model_class() with the decorated class.

    Returns:
        Decorator function
    """
    # Import here to avoid circular imports at module level
    from apflow.core.storage.sqlalchemy.models import TaskModel

    def decorator(cls: "TaskModel") -> "TaskModel":
        if not issubclass(cls, TaskModel):
            raise TypeError(
                f"Class {cls.__name__} must be a subclass of TaskModel. "
                f"Please ensure your class inherits from TaskModel:\n"
                f"  from apflow.core.storage.sqlalchemy.models import TaskModel\n"
                f"  class MyTaskModel(TaskModel):\n"
                f"      # Your custom fields here\n"
                f"      pass"
            )
        set_task_model_class(cls)
        logger.debug(f"Registered TaskModel class via decorator: {cls.__name__}")
        return cls

    return decorator


__all__ = [
    "ConfigRegistry",
    "get_config",
    "register_pre_hook",
    "register_post_hook",
    "set_task_model_class",
    "get_task_model_class",
    "get_pre_hooks",
    "get_post_hooks",
    "clear_config",
    "set_use_task_creator",
    "get_use_task_creator",
    "set_require_existing_tasks",
    "get_require_existing_tasks",
    "register_task_tree_hook",
    "get_task_tree_hooks",
    "task_model_register",
    "register_webhook_verify_hook",
    "get_webhook_verify_hook",
    "set_distributed_config",
    "get_distributed_config",
]
