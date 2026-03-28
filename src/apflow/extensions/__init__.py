"""
Extensions for apflow

Extensions contain production-ready implementations of task executors and other
optional functionality. Each extension is available via an extra dependency.

Extensions implement the Extension interface and are registered in the unified
ExtensionRegistry using globally unique IDs.

Extensions are automatically registered when imported via type-specific decorators:
- @executor_register() for executors
- @storage_register() for storage backends
- @hook_register() for hooks
"""

# Tools are imported lazily when needed (not auto-imported for performance)

# Auto-import storage extensions to trigger registration
try:
    from apflow.extensions.storage import sqlite_storage  # noqa: F401

    try:
        from apflow.extensions.storage import postgres_storage  # noqa: F401
    except ImportError:
        # PostgreSQL not available, skip
        pass
except ImportError:
    # Storage extensions may not be available, that's okay
    pass

# Auto-import hook extensions to trigger registration
try:
    from apflow.extensions.hooks import pre_execution_hook  # noqa: F401
    from apflow.extensions.hooks import post_execution_hook  # noqa: F401
except ImportError:
    # Hook extensions may not be available, that's okay
    pass

# NOTE: Executors are NO LONGER auto-imported here
# They are now auto-discovered via AST scanning (see scanner.py)
# and loaded lazily only when actually executed (lazy loading architecture)
#
# This eliminates the need to maintain manual imports and dramatically
# improves CLI startup performance by avoiding heavy dependency imports

__all__ = []
