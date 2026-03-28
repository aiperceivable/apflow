"""
Unified extension registry

This registry manages all extensions (executors, storage, hooks, etc.)
using globally unique IDs and category-based discovery.

Uses Protocol-based design to avoid circular dependencies with ExecutableTask.
"""

from typing import Dict, List, Optional, Type, Callable, Any
from apflow.core.extensions.base import Extension
from apflow.core.extensions.types import ExtensionCategory
from apflow.core.extensions.protocol import ExecutorFactory
from apflow.logger import get_logger

logger = get_logger(__name__)


class ExtensionRegistry:
    """
    Unified registry for all extension types

    This registry provides:
    - ID-based registration (globally unique)
    - Category + type-based discovery
    - Conflict detection and error reporting

    Architecture:
    - Primary index: id -> Extension (for precise lookup)
    - Category index: category -> type -> List[Extension] (for discovery)

    Example:
        registry = ExtensionRegistry()

        # Register extension
        registry.register(stdio_executor)

        # Lookup by ID
        executor = registry.get_by_id("stdio_executor")

        # Lookup by category and type
        executor = registry.get_by_type(ExtensionCategory.EXECUTOR, "stdio")
    """

    _instance: Optional["ExtensionRegistry"] = None
    _by_id: Dict[str, Extension] = {}
    _by_category: Dict[ExtensionCategory, Dict[str, List[Extension]]] = {}
    _factory_functions: Dict[str, ExecutorFactory] = {}
    _executor_classes: Dict[str, Type[Any]] = {}

    def __new__(cls):
        """Singleton pattern"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._by_id = {}
            cls._instance._by_category = {}
            cls._instance._factory_functions = {}
            cls._instance._executor_classes = {}
        return cls._instance

    def register(
        self,
        extension: Extension,
        executor_class: Optional[Type[Any]] = None,
        factory: Optional[Callable[[Dict[str, Any]], Any]] = None,
        override: bool = False,
    ) -> None:
        """
        Register an extension

        Args:
            extension: Extension instance to register (template for metadata)
            executor_class: Optional executor class for creating new instances
                           (required for executors that need per-task instantiation)
            factory: Optional factory function to create executor instances.
                     Signature: factory(inputs: Dict[str, Any]) -> ExecutableTask (or Any)
                     If provided, this will be used instead of executor_class.
            override: If True, always override any previous registration. If False and exists, registration is skipped.

        Raises:
            ValueError: If extension.id is already registered and override=False
            ValueError: If extension.id is empty
            ValueError: If extension.category is invalid

        Example:
            # Register with instance (for simple executors)
            registry = get_registry()
            registry.register(SystemInfoExecutor())

            # Register with class (for executors that need per-task instantiation)
            registry.register(
                CrewaiExecutorTemplate(),
                executor_class=CrewaiExecutor,
                factory=lambda inputs: CrewaiExecutor(**inputs)
            )
        """
        # Validate extension
        if not extension.id:
            raise ValueError("Extension must have a non-empty id")

        if not isinstance(extension.category, ExtensionCategory):
            raise ValueError(
                f"Extension category must be ExtensionCategory enum, got {type(extension.category)}"
            )

        # Check ID conflict
        if extension.id in self._by_id and not override:
            existing = self._by_id[extension.id]
            logger.warning(
                f"Extension ID '{extension.id}' is already registered by "
                f"{existing.__class__.__name__} (category: {existing.category.value}). "
                f"Use override=True to replace it, or use a different ID."
            )
            return existing

        # Register to primary index
        self._by_id[extension.id] = extension

        # Register to category index
        category = extension.category
        ext_type = extension.type or "default"

        if category not in self._by_category:
            self._by_category[category] = {}
        if ext_type not in self._by_category[category]:
            self._by_category[category][ext_type] = []

        # Add to type list (allow multiple extensions with same type)
        self._by_category[category][ext_type].append(extension)

        # Store executor class and factory for instantiation
        # Use Protocol-based check to avoid circular import
        if category == ExtensionCategory.EXECUTOR:
            # Check if extension implements ExecutorLike protocol (structural typing)
            # This works for ExecutableTask without importing it directly
            # Protocol check: verify extension has required methods
            if hasattr(extension, "execute") and hasattr(extension, "get_input_schema"):
                if factory:
                    self._factory_functions[extension.id] = factory
                elif executor_class:
                    self._executor_classes[extension.id] = executor_class
                else:
                    # If extension is already an ExecutorLike instance, use its class
                    self._executor_classes[extension.id] = extension.__class__

        logger.info(
            f"Registered extension '{extension.name}' "
            f"(id: {extension.id}, category: {category.value}, type: {ext_type})"
        )

    def get_by_id(self, extension_id: str) -> Optional[Extension]:
        """
        Get extension by globally unique ID

        Args:
            extension_id: Extension ID

        Returns:
            Extension instance, or None if not found

        Example:
            executor = registry.get_by_id("stdio_executor")
        """
        return self._by_id.get(extension_id)

    def get_by_type(self, category: ExtensionCategory, ext_type: str) -> Optional[Extension]:
        """
        Get extension by category and type

        Returns the first extension matching the category and type.
        Useful for lookup based on task schemas (e.g., {"type": "stdio"}).

        Args:
            category: Extension category
            ext_type: Extension type identifier

        Returns:
            Extension instance (template), or None if not found

        Example:
            # Task schema: {"type": "stdio", "method": "command"}
            executor_template = registry.get_by_type(ExtensionCategory.EXECUTOR, "stdio")
        """
        category_dict = self._by_category.get(category, {})
        extensions = category_dict.get(ext_type, [])
        return extensions[0] if extensions else None

    def create_executor_instance(
        self, extension_id: str, inputs: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> Optional[Any]:
        """
        Create a new executor instance for task execution

        This method creates a new executor instance based on the registered extension.
        Uses Protocol-based design to avoid circular dependencies.
        Used by TaskManager to get fresh executor instances for each task execution.

        Args:
            extension_id: Extension ID
            inputs: Optional input parameters for executor initialization
            **kwargs: Additional parameters (e.g., task_id for cancellation checking)

        Returns:
            Executor instance (implements ExecutorLike protocol), or None if not found or not an executor

        Example:
            executor = registry.create_executor_instance("stdio_executor", inputs={...}, task_id="task-123")
        """
        extension = self._by_id.get(extension_id)
        if not extension or extension.category != ExtensionCategory.EXECUTOR:
            return None

        # Strategy: Try inputs=inputs, **kwargs first (preferred for BaseTask and similar)
        # Then fallback to merged kwargs for executors that don't accept inputs parameter

        # Use factory function if available
        if extension_id in self._factory_functions:
            factory = self._factory_functions[extension_id]
            # Factory functions receive merged params
            executor_init_params = (inputs or {}).copy()
            executor_init_params.update(kwargs)
            return factory(executor_init_params)

        # Use executor class if available
        if extension_id in self._executor_classes:
            executor_class = self._executor_classes[extension_id]
            # Try inputs=inputs, **kwargs first (preferred for BaseTask)
            try:
                return executor_class(inputs=inputs or {}, **kwargs)
            except TypeError:
                # Fallback: try merged kwargs for executors that don't accept inputs parameter
                try:
                    executor_init_params = (inputs or {}).copy()
                    executor_init_params.update(kwargs)
                    return executor_class(**executor_init_params)
                except Exception as e:
                    logger.error(f"Failed to instantiate executor '{executor_class.__name__}': {e}")
                    raise
            except Exception as e:
                logger.error(f"Failed to instantiate executor '{executor_class.__name__}': {e}")
                raise

        # If extension is already an ExecutorLike instance, try to create a new one
        # Check if it has the required methods (structural typing via Protocol)
        if hasattr(extension, "execute") and hasattr(extension, "get_input_schema"):
            # Handle CategoryOverride wrapper: get the wrapped class
            executor_class = extension.__class__
            if hasattr(extension, "_wrapped"):
                # This is a CategoryOverride wrapper, get the wrapped executor class
                executor_class = extension._wrapped.__class__

            # Try inputs=inputs, **kwargs first (preferred for BaseTask)
            try:
                return executor_class(inputs=inputs or {}, **kwargs)
            except TypeError:
                # Fallback: try merged kwargs
                try:
                    executor_init_params = (inputs or {}).copy()
                    executor_init_params.update(kwargs)
                    return executor_class(**executor_init_params)
                except Exception as e:
                    logger.error(f"Failed to instantiate executor from template: {e}")
                    raise
            except Exception as e:
                logger.error(f"Failed to instantiate executor from template: {e}")
                raise

        return None

    def get_all_by_category(self, category: ExtensionCategory) -> List[Extension]:
        """
        Get all extensions in a category

        Args:
            category: Extension category

        Returns:
            List of all extensions in the category
        """
        result = []
        category_dict = self._by_category.get(category, {})
        for extensions in category_dict.values():
            result.extend(extensions)
        return result

    # Category-specific convenience methods

    def get_executor(self, executor_id: str) -> Optional[Extension]:
        """
        Get executor extension by ID (convenience method)

        Args:
            executor_id: Executor extension ID

        Returns:
            Extension if found and is an executor, None otherwise
        """
        extension = self.get_by_id(executor_id)
        if extension and extension.category == ExtensionCategory.EXECUTOR:
            return extension
        return None

    def list_executors(self) -> List[Extension]:
        """
        List all executor extensions (convenience method)

        Returns:
            List of all executor extensions
        """
        return self.get_all_by_category(ExtensionCategory.EXECUTOR)

    def get_storage(self, storage_id: str) -> Optional[Extension]:
        """
        Get storage extension by ID (convenience method)

        Args:
            storage_id: Storage extension ID

        Returns:
            Extension if found and is a storage backend, None otherwise
        """
        extension = self.get_by_id(storage_id)
        if extension and extension.category == ExtensionCategory.STORAGE:
            return extension
        return None

    def list_storage_backends(self) -> List[Extension]:
        """
        List all storage backend extensions (convenience method)

        Returns:
            List of all storage backend extensions
        """
        return self.get_all_by_category(ExtensionCategory.STORAGE)

    def get_hooks(self, hook_type: Optional[str] = None) -> List[Extension]:
        """
        Get hook extensions (convenience method)

        Args:
            hook_type: Optional hook type filter

        Returns:
            List of hook extensions (filtered by type if provided)
        """
        if hook_type:
            return self.get_all_by_type(ExtensionCategory.HOOK, hook_type)
        return self.get_all_by_category(ExtensionCategory.HOOK)

    def create_storage_instance(self, storage_id: str, **kwargs) -> Optional[Any]:
        """
        Create a storage backend instance

        Args:
            storage_id: Storage extension ID (e.g., "sqlite", "postgresql")
            **kwargs: Connection parameters for storage backend

        Returns:
            StorageBackend instance, or None if not found
        """
        extension = self.get_by_id(storage_id)
        if not extension or extension.category != ExtensionCategory.STORAGE:
            return None

        # Storage backends are typically singletons or stateless
        # Return the extension instance itself (it implements StorageBackend interface)
        return extension

    def create_hook_instance(self, hook_id: str) -> Optional[Any]:
        """
        Create a hook extension instance

        Args:
            hook_id: Hook extension ID

        Returns:
            HookExtension instance, or None if not found
        """
        extension = self.get_by_id(hook_id)
        if not extension or extension.category != ExtensionCategory.HOOK:
            return None

        # Hook extensions are typically singletons
        # Return the extension instance itself (it implements HookExtension interface)
        return extension

    def get_all_by_type(self, category: ExtensionCategory, ext_type: str) -> List[Extension]:
        """
        Get all extensions matching category and type

        Args:
            category: Extension category
            ext_type: Extension type

        Returns:
            List of extensions (may be multiple if same type)
        """
        category_dict = self._by_category.get(category, {})
        return category_dict.get(ext_type, [])

    def is_registered(self, extension_id: str) -> bool:
        """Check if an extension ID is registered"""
        return extension_id in self._by_id

    def unregister(self, extension_id: str) -> bool:
        """
        Unregister an extension by ID

        Args:
            extension_id: Extension ID to unregister

        Returns:
            True if unregistered, False if not found
        """
        if extension_id not in self._by_id:
            return False

        extension = self._by_id[extension_id]
        category = extension.category
        ext_type = extension.type or "default"

        # Remove from primary index
        del self._by_id[extension_id]

        # Remove from category index
        if category in self._by_category:
            if ext_type in self._by_category[category]:
                self._by_category[category][ext_type] = [
                    ext for ext in self._by_category[category][ext_type] if ext.id != extension_id
                ]
                # Clean up empty type lists
                if not self._by_category[category][ext_type]:
                    del self._by_category[category][ext_type]
            # Clean up empty category dicts
            if not self._by_category[category]:
                del self._by_category[category]

        # Remove from executor registries
        if extension_id in self._factory_functions:
            del self._factory_functions[extension_id]
        if extension_id in self._executor_classes:
            del self._executor_classes[extension_id]

        logger.info(f"Unregistered extension '{extension_id}'")
        return True

    def list_registered(self) -> Dict[str, Dict[str, Any]]:
        """
        List all registered extensions

        Returns:
            Dictionary mapping extension_id to extension info
        """
        return {
            ext_id: {
                "name": ext.name,
                "category": ext.category.value,
                "type": ext.type,
                "version": ext.version,
                "class": ext.__class__.__name__,
            }
            for ext_id, ext in self._by_id.items()
        }

    def list_by_category(self, category: ExtensionCategory) -> List[str]:
        """
        List all extension IDs in a category

        Args:
            category: Extension category

        Returns:
            List of extension IDs
        """
        return [ext.id for ext in self.get_all_by_category(category)]

    def add_executor_hook(self, executor_id: str, hook_type: str, hook_func: Callable) -> None:
        """
        Add hook to an already registered executor

        Note: Each executor can only have one hook of each type (pre_hook or post_hook).
        If a hook already exists, it will be replaced.

        Args:
            executor_id: Executor ID (e.g., "crewai_executor")
            hook_type: "pre_hook" or "post_hook"
            hook_func: Hook function

        Raises:
            ValueError: If executor_id is not found or hook_type is invalid
        """
        if hook_type not in ("pre_hook", "post_hook"):
            raise ValueError(f"Invalid hook_type: {hook_type}. Must be 'pre_hook' or 'post_hook'")

        extension = self.get_by_id(executor_id)
        if not extension:
            raise ValueError(f"Executor '{executor_id}' not found")

        if extension.category != ExtensionCategory.EXECUTOR:
            raise ValueError(f"Extension '{executor_id}' is not an executor")

        # Get executor class from registry
        executor_class = self._executor_classes.get(executor_id)
        if not executor_class:
            # Try to get from extension instance
            # The extension is an instance, so we can get its class
            if hasattr(extension, "__class__"):
                executor_class = extension.__class__
                # Store it for future use
                self._executor_classes[executor_id] = executor_class
            else:
                raise ValueError(
                    f"Cannot find executor class for '{executor_id}'. "
                    f"Make sure the executor was registered using @executor_register() decorator."
                )

        # Store hooks in executor class metadata
        if not hasattr(executor_class, "_executor_hooks"):
            executor_class._executor_hooks = {}

        executor_class._executor_hooks[hook_type] = hook_func
        logger.info(f"Added {hook_type} to executor '{executor_id}'")


# Global registry instance
_registry = ExtensionRegistry()
_extensions_loaded = False  # Track if extensions have been auto-loaded


def get_registry() -> ExtensionRegistry:
    """
    Get the global extension registry instance

    Auto-loads extensions on first access to ensure all extensions are registered.
    This lazy loading approach improves CLI startup performance while ensuring
    extensions are available when needed.

    Returns:
        ExtensionRegistry singleton instance
    """
    global _extensions_loaded

    # Auto-load extensions on first registry access
    if not _extensions_loaded:
        _extensions_loaded = True
        try:
            # Import extensions module which auto-registers all extensions
            import apflow.extensions  # noqa: F401

            logger.debug("Auto-loaded extensions on first registry access")
        except ImportError:
            # Extensions may not be installed
            logger.debug("Extensions not available (ImportError)")
        except Exception as e:
            # Log but don't fail if extensions have errors
            logger.warning(f"Failed to auto-load extensions: {e}")

    return _registry


def register_extension(extension: Extension, override: bool = False) -> None:
    """
    Register an extension (convenience function)

    Args:
        extension: Extension instance to register
        override: If True, always override any previous registration. If False and exists, registration is skipped.

    Example:
        from apflow.core.extensions import register_extension, ExtensionCategory

        from apflow.core.interfaces.executable_task import ExecutableTask
        from apflow.core.base import BaseTask

        class MyExecutor(BaseTask):
            id = "my_executor"
            name = "My Executor"
            type = "custom"
            ...

        register_extension(MyExecutor())
    """
    _registry.register(extension, override=override)


def add_executor_hook(executor_id: str, hook_type: str, hook_func: Callable) -> None:
    """
    Add hook to an already registered executor (convenience function)

    Args:
        executor_id: Executor ID (e.g., "crewai_executor")
        hook_type: "pre_hook" or "post_hook"
        hook_func: Hook function

    Example:
        from apflow.core.extensions.registry import add_executor_hook

        async def quota_check_hook(executor, task, inputs):
            if should_use_demo_data(task):
                return get_demo_data(task)
            return None

        add_executor_hook("crewai_executor", "pre_hook", quota_check_hook)
    """
    _registry.add_executor_hook(executor_id, hook_type, hook_func)


__all__ = [
    "ExtensionRegistry",
    "get_registry",
    "register_extension",
    "add_executor_hook",
]
