"""
PostgreSQL storage backend extension

Provides PostgreSQL database backend as an ExtensionCategory.STORAGE extension.
Optional - requires [postgres] extra installation.
"""

from typing import Dict, Any
from apflow.core.extensions.storage import StorageBackend
from apflow.core.extensions.decorators import storage_register

# Lazy registration - only register if PostgreSQL is available
try:
    # Check if PostgreSQL dependencies are available
    import psycopg2  # noqa: F401

    _POSTGRES_AVAILABLE = True
except ImportError:
    _POSTGRES_AVAILABLE = False


if _POSTGRES_AVAILABLE:

    @storage_register()
    class PostgreSQLStorage(StorageBackend):
        """
        PostgreSQL storage backend extension

        Provides PostgreSQL database support.
        Registered as ExtensionCategory.STORAGE extension.
        Requires [postgres] extra installation.
        """

        id = "postgresql"
        name = "PostgreSQL Storage"
        description = "PostgreSQL database backend (requires [postgres] extra)"
        version = "1.0.0"

        @property
        def type(self) -> str:
            """Extension type identifier"""
            return "postgresql"

        def normalize_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
            """
            Normalize data before writing to database

            PostgreSQL natively supports JSONB, can store directly.
            """
            # PostgreSQL's JSONB type can directly handle dict/list
            return data

        def denormalize_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
            """
            Denormalize data after reading from database
            """
            # PostgreSQL's JSONB automatically converts to Python dict/list
            return data

        def get_connection_string(self, **kwargs) -> str:
            """
            Generate PostgreSQL connection string

            Args:
                **kwargs: Connection parameters
                    - connection_string: Direct connection string (if provided, used as-is)
                    - user: Database user
                    - password: Database password
                    - host: Database host (default: localhost)
                    - port: Database port (default: 5432)
                    - database: Database name

            Returns:
                Connection string for SQLAlchemy
            """
            if "connection_string" in kwargs:
                return kwargs["connection_string"]

            user = kwargs.get("user", "postgres")
            password = kwargs.get("password", "")
            host = kwargs.get("host", "localhost")
            port = kwargs.get("port", 5432)
            database = kwargs.get("database", "postgres")

            return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{database}"

        def get_engine_kwargs(self) -> Dict[str, Any]:
            """PostgreSQL specific engine parameters"""
            return {
                "pool_size": 10,
                "max_overflow": 20,
                "pool_pre_ping": True,
                "pool_recycle": 3600,
            }

    __all__ = ["PostgreSQLStorage"]
else:
    __all__ = []
