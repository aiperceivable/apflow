"""
Extension registration decorators

Provides decorators for automatic extension registration.
Extensions can use these decorators to automatically register themselves when imported,
without requiring changes to core code.

Type-specific decorators:
- @executor_register() - for executors
- @storage_register() - for storage backends
- @hook_register() - for hooks
"""

from typing import Callable, Optional, Dict, Any, Type, TYPE_CHECKING
from apflow.core.extensions.registry import get_registry
from apflow.core.extensions.base import Extension
from apflow.core.extensions.protocol import ExecutorFactory
from apflow.logger import get_logger

if TYPE_CHECKING:
    from apflow.core.extensions.types import ExtensionCategory

logger = get_logger(__name__)


def _register_extension(
    cls: Type[Any],
    category: "ExtensionCategory",
    factory: Optional[ExecutorFactory] = None,
    override: bool = False,
) -> Type[Any]:
    """
    Internal helper function to register an extension

    Args:
        cls: Extension class to register
        category: Extension category
        factory: Optional factory function
        override: If True, always force override any previous registration. If False and exists, registration is skipped.

    Returns:
        Same class (for chaining)
    """
    # Validate that class implements Extension
    if not issubclass(cls, Extension):
        raise TypeError(
            f"Class {cls.__name__} must implement Extension interface "
            f"to use extension registration decorator"
        )

    # Create a template instance for metadata
    try:
        # Try to create instance with empty inputs
        template = cls(inputs={})
    except Exception as e:
        # If instantiation fails, create a minimal template using class attributes
        logger.debug(
            f"Could not create template instance for {cls.__name__}: {e}. "
            f"Using class attributes for registration."
        )

        # Create a minimal template class
        class TemplateClass(cls):
            """Template instance for registration"""

            def __init__(self):
                # Bypass parent __init__ to avoid errors
                pass

        # Set required attributes from class
        template = TemplateClass()
        template.id = getattr(cls, "id", cls.__name__.lower())
        template.name = getattr(cls, "name", cls.__name__)
        template.description = getattr(cls, "description", "")

    # Override category
    from apflow.core.extensions.types import ExtensionCategory

    # Validate category
    if not isinstance(category, ExtensionCategory):
        raise TypeError(f"category must be ExtensionCategory enum, got {type(category)}")

    # Create a wrapper that overrides the category property
    original_category = template.category

    class CategoryOverride:
        """Wrapper to override category property"""

        def __init__(self, wrapped, override_category):
            self._wrapped = wrapped
            self._category = override_category

        @property
        def category(self):
            return self._category

        def __getattr__(self, name):
            # Delegate all other attributes to wrapped object
            return getattr(self._wrapped, name)

        def __setattr__(self, name, value):
            # Handle our own attributes
            if name in ("_wrapped", "_category"):
                super().__setattr__(name, value)
            else:
                # Delegate to wrapped object
                setattr(self._wrapped, name, value)

    template = CategoryOverride(template, category)
    logger.debug(
        f"Category override: {original_category.value} -> {category.value} " f"for {cls.__name__}"
    )

    # Get registry and register
    registry = get_registry()

    # Determine factory function
    if factory:
        executor_factory = factory
    else:
        # Default factory: use class constructor
        # For executors that need special initialization (like CrewaiExecutor),
        # we try to pass inputs as kwargs first, then fallback to inputs parameter
        def default_factory(inputs: Dict[str, Any]) -> Any:
            try:
                # Try to instantiate with **inputs (for classes like CrewaiExecutor)
                # This allows passing task_id and other kwargs
                return cls(**inputs)
            except TypeError:
                # Fallback: separate inputs from other kwargs
                # Extract 'inputs' key if present, otherwise use all as inputs
                executor_inputs = inputs.pop("inputs", inputs) if "inputs" in inputs else inputs
                # Try with inputs parameter and remaining kwargs
                try:
                    return cls(inputs=executor_inputs, **inputs)
                except TypeError:
                    # Final fallback: just inputs parameter
                    return cls(inputs=executor_inputs)

        executor_factory = default_factory

    # Check if already registered and override is False
    ext_id = getattr(template, "id", cls.__name__.lower())
    if not override:
        # Check if extension is already registered in registry (by id and category)
        existing = registry.get_by_id(ext_id)
        if existing is not None and getattr(existing, "category", None) == category:
            logger.debug(
                f"Extension '{ext_id}' already registered in category '{category.value}'. "
                f"Returning previously registered class."
            )
            return getattr(existing, "executor_class", cls)

    # Register extension
    try:
        registry.register(
            extension=template, executor_class=cls, factory=executor_factory, override=override
        )
        logger.debug(
            f"Registered extension '{template.id}' "
            f"(category: {template.category.value}, type: {template.type})"
            f" (force override: {override})"
        )
    except Exception as e:
        logger.error(f"Failed to register extension {cls.__name__}: {e}", exc_info=True)
        # Don't raise - allow class to be used even if registration fails
        # This allows optional extensions to work without breaking imports

    return cls


def executor_register(
    factory: Optional[ExecutorFactory] = None,
    override: bool = False,
    pre_hook: Optional[Callable] = None,
    post_hook: Optional[Callable] = None,
):
    """
    Decorator for executor registration (type-specific)

    Usage:
        @executor_register()
        class MyExecutor(BaseTask):
            id = "my_executor"
            type = "my_type"
            ...

        # Or with custom factory
        @executor_register(factory=lambda inputs: MyExecutor(**inputs))
        class MyExecutor(BaseTask):
            ...

        # Or with pre/post hooks
        @executor_register(
            pre_hook=lambda executor, task, inputs: check_quota(executor, task, inputs),
            post_hook=lambda executor, task, inputs, result: log_execution(executor, task, result)
        )
        class MyExecutor(BaseTask):
            ...

    Args:
        factory: Optional factory function to create executor instances.
                Signature: factory(inputs: Dict[str, Any]) -> ExecutableTask
        override: If True, always force override any previous registration. If False and exists, registration is skipped.
        pre_hook: Optional hook function called before executor.execute().
                 Signature: async def pre_hook(executor, task, inputs) -> Optional[Dict[str, Any]]
                 If returns non-None, skips executor execution and uses returned value.
        post_hook: Optional hook function called after executor.execute().
                  Signature: async def post_hook(executor, task, inputs, result) -> None

    Returns:
        Decorated class (same class, registered automatically)

    Example:
        from apflow.core.extensions.decorators import executor_register
        from apflow.core.base import BaseTask

        @executor_register()
        class SystemInfoExecutor(BaseTask):
            id = "system_info_executor"
            name = "System Info Executor"
            type = "stdio"
            ...
    """

    def decorator(cls: Type[Any]) -> Type[Any]:
        from apflow.core.extensions.types import ExtensionCategory

        registered_cls = _register_extension(cls, ExtensionCategory.EXECUTOR, factory, override)

        # Store hooks in executor class metadata
        if not hasattr(registered_cls, "_executor_hooks"):
            registered_cls._executor_hooks = {}

        if pre_hook:
            registered_cls._executor_hooks["pre_hook"] = pre_hook
        if post_hook:
            registered_cls._executor_hooks["post_hook"] = post_hook

        return registered_cls

    return decorator


def storage_register(override: bool = False):
    """
    Decorator for storage backend registration (type-specific)

    Usage:
        @storage_register()
        class MyStorage(StorageBackend):
            id = "my_storage"
            type = "custom"
            ...

    Args:
        override: If True, always force override any previous registration. If False and exists, registration is skipped.

    Returns:
        Decorated class (same class, registered automatically)

    Example:
        from apflow.core.extensions.decorators import storage_register
        from apflow.core.extensions.storage import StorageBackend

        @storage_register()
        class SQLiteStorage(StorageBackend):
            id = "sqlite"
            name = "SQLite Storage"
            type = "sqlite"
            ...
    """

    def decorator(cls: Type[Any]) -> Type[Any]:
        from apflow.core.extensions.types import ExtensionCategory

        return _register_extension(cls, ExtensionCategory.STORAGE, None, override)

    return decorator


def hook_register(override: bool = False):
    """
    Decorator for hook registration (type-specific)

    Usage:
        @hook_register()
        class MyHook(HookExtension):
            id = "my_hook"
            type = "pre_execution"
            ...

    Args:
        override: If True, always force override any previous registration. If False and exists, registration is skipped.

    Returns:
        Decorated class (same class, registered automatically)

    Example:
        from apflow.core.extensions.decorators import hook_register
        from apflow.core.extensions.hook import HookExtension

        @hook_register()
        class PreExecutionHook(HookExtension):
            id = "pre_exec_hook"
            name = "Pre-Execution Hook"
            type = "pre_execution"
            ...
    """

    def decorator(cls: Type[Any]) -> Type[Any]:
        from apflow.core.extensions.types import ExtensionCategory

        return _register_extension(cls, ExtensionCategory.HOOK, None, override)

    return decorator


__all__ = [
    "executor_register",
    "storage_register",
    "hook_register",
]
