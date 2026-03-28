"""
Storage extensions

Provides storage backend implementations as ExtensionCategory.STORAGE extensions.
"""

# Auto-register storage backends when imported
from apflow.extensions.storage import sqlite_storage  # noqa: F401

try:
    from apflow.extensions.storage import postgres_storage  # noqa: F401
except ImportError:
    # PostgreSQL not available, skip
    pass

__all__ = ["sqlite_storage"]
