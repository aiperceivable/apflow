"""
Extension management for apflow

This module handles initialization and configuration of optional extensions.
Extensions are automatically detected using AST-based scanning (no imports).
"""

import os
from typing import Any, Optional, Dict
from apflow.core.execution.errors import ExecutorError
from apflow.core.extensions.scanner import ExtensionScanner
from apflow.logger import get_logger

logger = get_logger(__name__)


def _is_package_installed(package_name: str) -> bool:
    """
    Check if a package is installed WITHOUT importing it (lazy loading safe)

    Uses importlib.metadata to check installed distributions without importing.
    Also detects stdlib/builtins via module spec resolution.

    Args:
        package_name: Package name to check (e.g., "httpx", "docker")

    Returns:
        True if package is installed, False otherwise
    """
    # Fast path for stdlib/builtins without importing
    try:
        import sys
        import importlib.util

        normalized = package_name.split(".")[0]
        if normalized in sys.builtin_module_names:
            return True

        if hasattr(sys, "stdlib_module_names") and normalized in sys.stdlib_module_names:
            return True

        if importlib.util.find_spec(normalized) is not None:
            return True
    except Exception:
        pass

    try:
        # Python 3.8+ has importlib.metadata in stdlib
        from importlib.metadata import distributions
    except ImportError:
        # Python 3.7 fallback (shouldn't be needed as we require 3.10+)
        try:
            from importlib_metadata import distributions
        except ImportError:
            return False

    # Check all installed distributions WITHOUT importing
    package_normalized = package_name.lower().replace("-", "_")
    for dist in distributions():
        # Normalize package name (handle case differences, hyphens vs underscores)
        dist_name = dist.metadata.get("Name", "").lower().replace("-", "_")

        if dist_name == package_normalized:
            return True

    return False


def get_extension_env() -> Optional[str]:
    """
    Get the value of the APFLOW_EXTENSIONS environment variable

    Returns:
        The value of APFLOW_EXTENSIONS, or None if not set
    """
    return os.getenv("APFLOW_EXTENSIONS")


def get_allowed_executor_ids() -> Optional[set[str]]:
    """
    Get the set of allowed executor IDs from environment configuration

    Supports two environment variables:
    - APFLOW_EXTENSIONS: Extension directory names (e.g., "core", "http", "email")
    - APFLOW_EXTENSIONS_IDS: Specific executor IDs (e.g., "rest_executor", "send_email_executor")

    If either is set, only those executor IDs are allowed.
    This provides security control to restrict which executors users can access.

    Returns:
        Set of allowed executor IDs, or None if no restrictions (allow all)

    Examples:
        APFLOW_EXTENSIONS=core,http -> All executors from core and http extensions
        APFLOW_EXTENSIONS_IDS=rest_executor,send_email_executor -> Only these specific executors
        Both set -> Union of executors from extensions + specific IDs
        Neither set -> All executors allowed (no restrictions)
    """
    extensions_env = get_extension_env()
    extensions_ids_env = os.environ.get("APFLOW_EXTENSIONS_IDS")

    # If neither is set, allow all
    if not extensions_env and not extensions_ids_env:
        return None

    allowed_ids: set[str] = set()

    # Process APFLOW_EXTENSIONS (extension directory names)
    if extensions_env:
        extensions_env = extensions_env.strip()
        extension_names = [e.strip() for e in extensions_env.split(",") if e.strip()]

        for extension_name in extension_names:
            extension_executor_ids = ExtensionScanner.get_executor_ids_by_extension(extension_name)
            if extension_executor_ids:
                allowed_ids.update(extension_executor_ids)
            else:
                logger.warning(f"Unknown extension in APFLOW_EXTENSIONS: '{extension_name}'")

    # Process APFLOW_EXTENSIONS_IDS (specific executor IDs)
    if extensions_ids_env:
        extensions_ids_env = extensions_ids_env.strip()
        executor_ids = [e.strip() for e in extensions_ids_env.split(",") if e.strip()]

        all_executor_ids = set(ExtensionScanner.get_all_executor_ids())
        for executor_id in executor_ids:
            if executor_id in all_executor_ids:
                allowed_ids.add(executor_id)
            else:
                logger.warning(f"Unknown executor ID in APFLOW_EXTENSIONS_IDS: '{executor_id}'")

    # A restriction was explicitly configured (extensions_env and/or
    # extensions_ids_env is set). If every entry turned out to be invalid,
    # fail closed by returning the empty set (deny all) rather than falling
    # back to None (allow all) — an empty allow-list here is almost always a
    # misconfiguration (e.g. a typo), and failing open would silently defeat
    # the administrator's intent to restrict which executors can run.
    return allowed_ids


# Track whether all extensions have been loaded
_all_extensions_loaded = False

_loaded_extensions: Dict[str, bool] = {}  # Track which extensions have been loaded


def load_extension_by_name(extension_name: str) -> None:
    """
    Load all executors from a specific extension by directory name

    Args:
        extension_name: Extension directory name (e.g., "stdio", "http", "core")

    Raises:
        ValueError: If extension directory is unknown

    Example:
        load_extension_by_name("stdio")  # Loads system_info_executor, command_executor
        load_extension_by_name("core")   # Loads aggregate_results_executor
    """
    global _loaded_extensions

    # Get all executor IDs in this extension
    executor_ids = ExtensionScanner.get_executor_ids_by_extension(extension_name)

    if not executor_ids:
        # Check if it's a known extension directory but has no executors
        # This should rarely happen, but handle gracefully
        logger.warning(f"Extension '{extension_name}' has no executors or is unknown")
        return

    # Load each executor in the extension
    loaded_count = 0
    for executor_id in executor_ids:
        try:
            load_extension_by_id(executor_id)
            loaded_count += 1
        except ExecutorError as e:
            logger.warning(f"Skipped executor '{executor_id}' in extension '{extension_name}': {e}")

    _loaded_extensions[extension_name] = True
    logger.debug(
        f"Loaded {loaded_count}/{len(executor_ids)} executors from extension '{extension_name}'"
    )


def load_extension_by_id(executor_id: str) -> None:
    """
    Load extension for a specific executor ID (LAZY - imports only when called)

    This is called ONLY when actually executing a task with that executor.
    CLI --help or listing executors does NOT trigger this function.

    Uses ExtensionScanner to get metadata (no import), then imports only
    the specific executor module when dependencies are satisfied.

    Args:
        executor_id: Executor ID (e.g., "system_info_executor", "rest_executor")

    Raises:
        ExecutorError: If executor not found or dependencies missing
    """
    metadata = ExtensionScanner.get_executor_metadata(executor_id)

    if not metadata:
        raise ExecutorError(f"Unknown executor: {executor_id}")

    from apflow.core.extensions import get_registry

    registry = get_registry()
    if registry.is_registered(executor_id):
        logger.debug(f"Executor '{executor_id}' already loaded")
        return

    if not metadata.always_available:
        missing_deps = [dep for dep in metadata.dependencies if not _is_package_installed(dep)]
        if missing_deps:
            raise ExecutorError(f"Executor '{executor_id}' requires: {', '.join(missing_deps)}")

    logger.debug(f"Lazy loading executor '{executor_id}' from {metadata.module_path}")

    try:
        module = __import__(metadata.module_path, fromlist=[metadata.class_name])

        if not registry.is_registered(executor_id):
            executor_class = getattr(module, metadata.class_name)
            from apflow.core.extensions.decorators import _register_extension
            from apflow.core.extensions.types import ExtensionCategory

            _register_extension(executor_class, ExtensionCategory.EXECUTOR, override=True)

        logger.info(f"Loaded executor '{executor_id}'")

    except Exception as e:
        logger.error(f"Failed to load executor '{executor_id}': {e}")
        raise ExecutorError(f"Failed to load executor '{executor_id}': {e}")


def _load_all_extensions() -> None:
    """
    Load all available extensions (LAZY - only on-demand)

    Scans extensions directory using AST (no imports) and loads executors
    based on APFLOW_EXTENSIONS environment variable.

    This function is called when listing available executors to ensure
    the registry contains all available executors.
    """
    ExtensionScanner.scan_builtin_executors()

    extensions_env = get_extension_env()
    if extensions_env:
        enabled_ids = [e.strip() for e in extensions_env.split(",") if e.strip()]
        executor_ids_to_load = enabled_ids
        logger.debug(f"APFLOW_EXTENSIONS set, loading: {executor_ids_to_load}")
    else:
        executor_ids_to_load = ExtensionScanner.get_all_executor_ids()
        logger.debug(f"Loading all {len(executor_ids_to_load)} discovered executors")

    for executor_id in executor_ids_to_load:
        try:
            load_extension_by_id(executor_id)
        except ExecutorError as e:
            logger.warning(f"Skipped executor '{executor_id}': {e}")


def get_available_executors() -> dict[str, Any]:
    """
    Get list of available executors (NO IMPORTS - fast)

    This function returns metadata for all executors without importing them.
    Used by CLI --help and API listing endpoints for fast startup.

    Returns:
        Dictionary with:
            - executors: List of available executor metadata
            - restricted: Boolean indicating if access is restricted by APFLOW_EXTENSIONS
            - allowed_ids: List of allowed executor IDs (if restricted)
    """
    ExtensionScanner.scan_builtin_executors()

    all_executor_ids = ExtensionScanner.get_all_executor_ids()

    allowed_executor_ids = get_allowed_executor_ids()
    is_restricted = allowed_executor_ids is not None

    if is_restricted:
        available_ids = [
            eid for eid in all_executor_ids if allowed_executor_ids and eid in allowed_executor_ids
        ]
    else:
        available_ids = all_executor_ids

    executors_list = []
    for executor_id in available_ids:
        metadata = ExtensionScanner.get_executor_metadata(executor_id)
        if metadata:
            executors_list.append(
                {
                    "id": metadata.id,
                    "name": metadata.name,
                    "description": metadata.description,
                    "tags": metadata.tags,
                    "dependencies": metadata.dependencies,
                    "available": metadata.always_available
                    or all(_is_package_installed(dep) for dep in metadata.dependencies),
                }
            )

    result = {
        "executors": executors_list,
        "count": len(executors_list),
        "restricted": is_restricted,
    }

    if is_restricted and allowed_executor_ids:
        result["allowed_ids"] = sorted(allowed_executor_ids)

    return result


def _ensure_extension_registered(executor_class: Any, extension_id: str) -> None:
    """
    Ensure an extension is registered in the registry

    This function checks if an extension is already registered, and if not,
    manually registers it. This handles the case where modules were imported
    before but the registry was cleared (e.g., in tests).

    Args:
        executor_class: Executor class to register
        extension_id: Expected extension ID
    """
    from apflow.core.extensions import get_registry

    registry = get_registry()

    # If already registered, nothing to do
    if registry.is_registered(extension_id):
        return

    # Module was imported but extension not registered (e.g., registry was cleared)
    # Manually register it
    try:
        from apflow.core.extensions.decorators import _register_extension
        from apflow.core.extensions.types import ExtensionCategory

        _register_extension(executor_class, ExtensionCategory.EXECUTOR, override=True)
        logger.debug(f"Manually registered extension '{extension_id}'")
    except Exception as reg_error:
        logger.warning(f"Failed to manually register {executor_class.__name__}: {reg_error}")


def _load_custom_task_model() -> None:
    """Load custom TaskModel class from environment variable if specified"""
    task_model_class_path = os.getenv("APFLOW_TASK_MODEL_CLASS")
    if task_model_class_path:
        try:
            from importlib import import_module

            from apflow.core.config.registry import set_task_model_class

            module_path, class_name = task_model_class_path.rsplit(".", 1)
            module = import_module(module_path)
            task_model_class = getattr(module, class_name)
            set_task_model_class(task_model_class)
            logger.info(f"Loaded custom TaskModel: {task_model_class_path}")
        except Exception as e:
            logger.warning(f"Failed to load custom TaskModel from {task_model_class_path}: {e}")


def initialize_extensions() -> None:
    """
    Initialize apflow extensions intelligently

    """
    logger.info("Initializing apflow extensions...")

    _load_all_extensions()
    # Load custom TaskModel
    _load_custom_task_model()

    logger.info("Extension initialization completed")
