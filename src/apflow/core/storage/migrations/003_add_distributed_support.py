"""
Migration: Add distributed execution support

This migration adds distributed task orchestration support:
1. 6 nullable columns on apflow_tasks for distributed execution
2. 5 new tables (PostgreSQL only) for cluster coordination:
   - apflow_distributed_nodes: Node registry with health tracking
   - apflow_task_leases: Task-to-node lease bindings
   - apflow_execution_idempotency: Idempotency tracking
   - apflow_cluster_leader: Leader election singleton
   - apflow_task_events: Audit log for task lifecycle events

SQLite: Only adds the 6 nullable columns (distributed tables are PostgreSQL-only).

File: 003_add_distributed_support.py
ID: 003_add_distributed_support (auto-extracted from filename)
"""

from sqlalchemy import Engine, inspect as sa_inspect, text
from apflow.core.storage.migrations import Migration
from apflow.core.storage.sqlalchemy.models import TASK_TABLE_NAME
from apflow.logger import get_logger

logger = get_logger(__name__)

DISTRIBUTED_TABLES = [
    "apflow_distributed_nodes",
    "apflow_task_leases",
    "apflow_execution_idempotency",
    "apflow_cluster_leader",
    "apflow_task_events",
]

TASK_MODEL_COLUMNS = {
    "lease_id": "VARCHAR(100)",
    "lease_expires_at": "TIMESTAMP WITH TIME ZONE",
    "placement_constraints": "JSON",
    "attempt_id": "INTEGER DEFAULT 0",
    "idempotency_key": "VARCHAR(255)",
    "last_assigned_node": "VARCHAR(100)",
}


class AddDistributedSupport(Migration):
    """Add distributed execution support tables and columns."""

    aliases = ["add_distributed_support"]
    description = (
        "Add distributed execution support: 5 cluster tables (PostgreSQL) "
        "and 6 nullable columns on apflow_tasks"
    )

    def _get_existing_columns(self, engine: Engine, table: str) -> set[str]:
        """Get existing column names for a table."""
        try:
            inspector = sa_inspect(engine)
            return {col["name"] for col in inspector.get_columns(table)}
        except Exception as e:
            logger.warning(f"Could not get columns for '{table}': {e}")
            return set()

    def _table_exists(self, engine: Engine, table: str) -> bool:
        """Check if a table exists."""
        try:
            inspector = sa_inspect(engine)
            return table in inspector.get_table_names()
        except Exception:
            return False

    def _is_postgresql(self, engine: Engine) -> bool:
        """Check if the engine is PostgreSQL."""
        return engine.dialect.name == "postgresql"

    def _add_task_model_columns(self, engine: Engine) -> None:
        """Add 6 distributed fields to the tasks table."""
        table_name = TASK_TABLE_NAME
        if not self._table_exists(engine, table_name):
            logger.debug(f"Table '{table_name}' does not exist, skipping")
            return

        existing_columns = self._get_existing_columns(engine, table_name)
        for col_name, col_type in TASK_MODEL_COLUMNS.items():
            if col_name not in existing_columns:
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text(f"ALTER TABLE {table_name} " f"ADD COLUMN {col_name} {col_type}")
                        )
                    logger.info(f"  {self.id}: Added column '{col_name}' " f"to '{table_name}'")
                except Exception as e:
                    logger.error(f"  {self.id}: Failed to add column " f"'{col_name}': {e}")
                    raise

    def _create_distributed_tables(self, engine: Engine) -> None:
        """Create the 5 distributed tables (PostgreSQL only)."""
        table_name = TASK_TABLE_NAME

        ddl_statements = [
            """CREATE TABLE IF NOT EXISTS apflow_distributed_nodes (
                node_id VARCHAR(100) PRIMARY KEY,
                executor_types JSON NOT NULL,
                capabilities JSON DEFAULT '{}'::json,
                status VARCHAR(20) NOT NULL,
                heartbeat_at TIMESTAMP WITH TIME ZONE NOT NULL,
                registered_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )""",
            f"""CREATE TABLE IF NOT EXISTS apflow_task_leases (
                task_id VARCHAR(100) PRIMARY KEY
                    REFERENCES {table_name}(id),
                node_id VARCHAR(100) NOT NULL
                    REFERENCES apflow_distributed_nodes(node_id),
                lease_token VARCHAR(100) NOT NULL UNIQUE,
                acquired_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                attempt_id INTEGER DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS apflow_execution_idempotency (
                task_id VARCHAR(100) NOT NULL,
                attempt_id INTEGER NOT NULL,
                idempotency_key VARCHAR(255) NOT NULL UNIQUE,
                result JSON,
                status VARCHAR(20) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                PRIMARY KEY (task_id, attempt_id)
            )""",
            """CREATE TABLE IF NOT EXISTS apflow_cluster_leader (
                leader_id VARCHAR(100) PRIMARY KEY DEFAULT 'singleton',
                node_id VARCHAR(100) NOT NULL
                    REFERENCES apflow_distributed_nodes(node_id),
                lease_token VARCHAR(100) NOT NULL UNIQUE,
                acquired_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL
            )""",
            f"""CREATE TABLE IF NOT EXISTS apflow_task_events (
                event_id VARCHAR(100) PRIMARY KEY,
                task_id VARCHAR(100) NOT NULL
                    REFERENCES {table_name}(id) ON DELETE CASCADE,
                event_type VARCHAR(50) NOT NULL,
                node_id VARCHAR(100),
                details JSON DEFAULT '{{}}'::json,
                "timestamp" TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )""",
        ]

        for ddl in ddl_statements:
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
            except Exception as e:
                logger.error(f"  {self.id}: Failed to create table: {e}")
                raise

        logger.info(f"  {self.id}: Created {len(ddl_statements)} distributed tables")

    def _create_indexes(self, engine: Engine) -> None:
        """Create indexes for distributed tables (PostgreSQL only)."""
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_task_leases_expires_at "
            "ON apflow_task_leases(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_task_leases_node_id " "ON apflow_task_leases(node_id)",
            "CREATE INDEX IF NOT EXISTS idx_distributed_nodes_status "
            "ON apflow_distributed_nodes(status)",
            "CREATE INDEX IF NOT EXISTS idx_distributed_nodes_heartbeat "
            "ON apflow_distributed_nodes(heartbeat_at)",
            "CREATE INDEX IF NOT EXISTS idx_task_events_task_timestamp "
            'ON apflow_task_events(task_id, "timestamp")',
        ]
        try:
            with engine.begin() as conn:
                for idx_sql in indexes:
                    conn.execute(text(idx_sql))
            logger.info(f"  {self.id}: Created distributed indexes")
        except Exception as e:
            logger.warning(f"  {self.id}: Could not create all indexes: {e}")

    def upgrade(self, engine: Engine) -> None:
        """Apply migration: add columns + create tables (PostgreSQL only)."""
        # Add 6 nullable columns to apflow_tasks (all dialects)
        self._add_task_model_columns(engine)

        # Create distributed tables + indexes (PostgreSQL only)
        if self._is_postgresql(engine):
            self._create_distributed_tables(engine)
            self._create_indexes(engine)
        else:
            logger.info(
                f"  {self.id}: Skipping distributed tables "
                f"on {engine.dialect.name} (PostgreSQL only)"
            )

    def downgrade(self, engine: Engine) -> None:
        """Rollback: drop distributed columns and tables."""
        table_name = TASK_TABLE_NAME

        # Drop columns from apflow_tasks
        if self._table_exists(engine, table_name):
            existing_columns = self._get_existing_columns(engine, table_name)
            for col_name in TASK_MODEL_COLUMNS:
                if col_name in existing_columns:
                    try:
                        with engine.begin() as conn:
                            conn.execute(
                                text(f"ALTER TABLE {table_name} " f"DROP COLUMN {col_name}")
                            )
                        logger.info(f"  Downgrade {self.id}: " f"Dropped column '{col_name}'")
                    except Exception as e:
                        logger.warning(
                            f"  Downgrade {self.id}: " f"Could not drop column '{col_name}': {e}"
                        )

        # Drop distributed tables (PostgreSQL only, reverse order for FK)
        if self._is_postgresql(engine):
            for table in reversed(DISTRIBUTED_TABLES):
                try:
                    with engine.begin() as conn:
                        conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
                    logger.info(f"  Downgrade {self.id}: Dropped table '{table}'")
                except Exception as e:
                    logger.warning(
                        f"  Downgrade {self.id}: " f"Could not drop table '{table}': {e}"
                    )

        logger.info(f"Downgrade {self.id}: Completed")
