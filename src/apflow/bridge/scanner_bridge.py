"""
Bridge between apflow's AST extension scanner and apcore module registration.

Uses ExtensionScanner to discover all @executor_register decorated classes,
then wraps each as an ExecutableTaskModuleAdapter for apcore.
"""

import importlib
from typing import Any, Dict

from apflow.bridge.module_adapter import ExecutableTaskModuleAdapter
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
    except (ImportError, AttributeError) as e:
        logger.warning(f"Cannot import executor {executor_id} ({metadata.module_path}): {e}")
        return None

    input_schema = _extract_schema(executor_class, "get_input_schema")
    output_schema = _extract_schema(executor_class, "get_output_schema")

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
    )


def _extract_schema(executor_class: type, method_name: str) -> Dict[str, Any]:
    """Try to extract JSON schema from an executor class.

    Attempts to instantiate the class to call the schema method.
    Falls back to a generic schema if instantiation fails.
    """
    try:
        instance = executor_class()
        schema_method = getattr(instance, method_name, None)
        if schema_method and callable(schema_method):
            return schema_method()
    except Exception:
        pass

    # Fallback: try with empty inputs
    try:
        instance = executor_class(inputs={})
        schema_method = getattr(instance, method_name, None)
        if schema_method and callable(schema_method):
            return schema_method()
    except Exception:
        pass

    return _FALLBACK_SCHEMA.copy()
