"""
Bridge between apflow's AST extension scanner and apcore module registration.

Uses ExtensionScanner to discover all @executor_register decorated classes,
then wraps each as an ExecutableTaskModuleAdapter for apcore.
"""

import importlib
from typing import Any, Dict

from apflow.bridge.module_adapter import ExecutableTaskModuleAdapter
from apflow.core.extensions.registry import get_registry
from apflow.core.extensions.scanner import ExtensionScanner
from apflow.logger import get_logger

logger = get_logger(__name__)

# Fallback schema for executors that can't be instantiated for schema extraction
_FALLBACK_SCHEMA: Dict[str, Any] = {"type": "object", "properties": {}}


def discover_executor_modules() -> list[ExecutableTaskModuleAdapter]:
    """Discover all registered executors and create apcore Module adapters.

    Uses the AST scanner (zero-import) for discovery, then imports each
    executor class to extract schemas.

    Returns:
        List of ExecutableTaskModuleAdapter instances.
    """
    scanner = ExtensionScanner()
    metadata_map = scanner.scan_builtin_executors()
    adapters: list[ExecutableTaskModuleAdapter] = []

    for executor_id, metadata in metadata_map.items():
        adapter = _create_adapter_from_metadata(executor_id, metadata)
        if adapter is not None:
            adapters.append(adapter)

    # Discover executors from entry points (third-party packages)
    try:
        from importlib.metadata import entry_points

        eps = entry_points(group="apflow.executors")
        for ep in eps:
            try:
                register_fn = ep.load()
                register_fn()  # Expected to call @function_executor or @executor_register
            except Exception as e:
                logger.warning(f"Failed to load executor entry point '{ep.name}': {e}")
    except Exception as e:
        # Surface group-level discovery failures (e.g. broken distribution metadata)
        # rather than silently dropping all entry-point executors.
        logger.warning(f"Failed to enumerate 'apflow.executors' entry points: {e}")

    registry = get_registry()
    seen_ids = {adapter.executor_id for adapter in adapters}
    for executor in registry.list_executors():
        executor_class = registry.get_executor_class(executor.id)
        if executor.id in seen_ids or executor_class is None:
            continue
        adapter = _create_adapter_from_class(executor.id, executor_class)
        if adapter is not None:
            adapters.append(adapter)
            seen_ids.add(executor.id)

    logger.info(f"Discovered {len(adapters)} executor modules for apcore registration")
    return adapters


def _create_adapter_from_metadata(
    executor_id: str, metadata: Any
) -> ExecutableTaskModuleAdapter | None:
    """Create an adapter from AST-scanned executor metadata.

    Imports the executor class and extracts input/output schemas.
    Returns None if import or schema extraction fails.
    """
    try:
        module = importlib.import_module(metadata.module_path)
        executor_class = getattr(module, metadata.class_name)
    except Exception as e:
        # Importing a module can run its @executor_register() decorator at
        # class-definition time, which raises TypeError for a class that
        # doesn't implement the Extension interface — not just ImportError
        # or AttributeError. One broken discovered executor must not crash
        # discovery/bootstrap for every other executor; skip it instead.
        logger.warning(f"Cannot import executor {executor_id} ({metadata.module_path}): {e}")
        return None

    fallback_status: list[bool] = []
    input_schema = _extract_schema(executor_class, "get_input_schema", fallback_status)
    output_schema = _extract_schema(executor_class, "get_output_schema", fallback_status)

    return ExecutableTaskModuleAdapter(
        executor_class=executor_class,
        executor_id=executor_id,
        executor_name=metadata.name,
        executor_description=metadata.description,
        input_schema=input_schema,
        output_schema=output_schema,
        tags=metadata.tags if hasattr(metadata, "tags") else [],
        dependencies=metadata.dependencies if hasattr(metadata, "dependencies") else [],
        always_available=(
            metadata.always_available if hasattr(metadata, "always_available") else True
        ),
        schema_fallback=any(fallback_status),
        schema_version=getattr(executor_class, "schema_version", None),
    )


def _extract_schema(
    executor_class: type, method_name: str, fallback_status: list[bool] | None = None
) -> Dict[str, Any]:
    """Try to extract JSON schema from an executor class.

    Attempts to instantiate the class to call the schema method.
    Falls back to a generic schema if instantiation fails.
    """
    try:
        instance = executor_class()
        schema_method = getattr(instance, method_name, None)
        if schema_method and callable(schema_method):
            schema = schema_method()
            if isinstance(schema, dict):
                return schema
    except Exception as e:
        logger.debug(f"Cannot instantiate {executor_class.__name__}(): {e}")

    # Fallback: try with empty inputs
    try:
        instance = executor_class(inputs={})
        schema_method = getattr(instance, method_name, None)
        if schema_method and callable(schema_method):
            schema = schema_method()
            if isinstance(schema, dict):
                return schema
    except Exception as e:
        logger.debug(
            f"Cannot instantiate {executor_class.__name__}(inputs={{}}): {e}, using fallback schema"
        )

    import copy

    if fallback_status is not None:
        fallback_status.append(True)
    return copy.deepcopy(_FALLBACK_SCHEMA)


def _create_adapter_from_class(
    executor_id: str, executor_class: type
) -> ExecutableTaskModuleAdapter | None:
    """Create adapter directly from a class (no AST metadata needed).

    Used for runtime-registered function executors.
    """
    try:
        fallback_status: list[bool] = []
        input_schema = _extract_schema(executor_class, "get_input_schema", fallback_status)
        output_schema = _extract_schema(executor_class, "get_output_schema", fallback_status)

        return ExecutableTaskModuleAdapter(
            executor_class=executor_class,
            executor_id=executor_id,
            executor_name=getattr(executor_class, "name", executor_id),
            executor_description=getattr(executor_class, "description", ""),
            input_schema=input_schema,
            output_schema=output_schema,
            tags=getattr(executor_class, "tags", []),
            schema_fallback=any(fallback_status),
            schema_version=getattr(executor_class, "schema_version", None),
        )
    except Exception as e:
        logger.warning(f"Cannot create adapter for function executor {executor_id}: {e}")
        return None
