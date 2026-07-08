"""
Database dialect registry
"""

from typing import Dict, Type, Protocol
from apflow.core.storage.dialects.sqlite import SQLiteDialect


# Dialect protocol
class DialectConfig(Protocol):
    """Database dialect configuration interface"""

    @staticmethod
    def normalize_data(data: Dict) -> Dict: ...

    @staticmethod
    def denormalize_data(data: Dict) -> Dict: ...

    @staticmethod
    def get_connection_string(**kwargs) -> str: ...

    @staticmethod
    def get_engine_kwargs() -> Dict: ...


# Dialect registry
_DIALECT_REGISTRY: Dict[str, Type] = {}


def register_dialect(name: str, dialect_class: Type):
    """Register database dialect"""
    _DIALECT_REGISTRY[name] = dialect_class


def get_dialect_config(name: str):
    """Get database dialect configuration instance"""
    if name not in _DIALECT_REGISTRY:
        raise ValueError(
            f"Unsupported dialect: {name}. " f"Available: {list(_DIALECT_REGISTRY.keys())}"
        )
    return _DIALECT_REGISTRY[name]


def _postgres_drivers_available() -> bool:
    """Check whether the drivers installed by the [postgres] extra are importable.

    postgres.py itself has no driver imports (it only builds connection
    strings/kwargs), so importing it always succeeds regardless of whether
    psycopg2/asyncpg are installed — that import alone can never raise
    ImportError. The actual driver dependency is only pulled in later, deep
    inside SQLAlchemy's create_engine()/create_async_engine(), so probe for
    it explicitly here instead.
    """
    try:
        import asyncpg  # noqa: F401
        import psycopg2  # noqa: F401

        return True
    except ImportError:
        return False


# Register built-in dialects
register_dialect("sqlite", SQLiteDialect)

# Lazy register PostgreSQL only when its drivers are actually installed
if _postgres_drivers_available():
    from apflow.core.storage.dialects.postgres import PostgreSQLDialect

    register_dialect("postgresql", PostgreSQLDialect)
    register_dialect("postgres", PostgreSQLDialect)  # Alias
