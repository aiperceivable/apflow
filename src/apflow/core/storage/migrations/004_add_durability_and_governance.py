"""
Migration: Add durability fields and task_checkpoints table (F-003/F-004)

This migration:
1. Adds 6 durability columns to apflow_tasks (checkpoint, retry, backoff)
2. Creates task_checkpoints table for checkpoint data storage

File: 004_add_durability_and_governance.py
ID: 004_add_durability_and_governance (auto-extracted from filename)
"""

from sqlalchemy import Engine, inspect as sa_inspect, text
from apflow.core.storage.migrations import Migration
from apflow.core.storage.sqlalchemy.models import TASK_TABLE_NAME
from apflow.logger import get_logger

logger = get_logger(__name__)

DURABILITY_COLUMNS = {
    "checkpoint_at": "TIMESTAMP",
    "resume_from": "VARCHAR(255)",
    "attempt_count": "INTEGER DEFAULT 0",
    "max_attempts": "INTEGER DEFAULT 3",
    "backoff_strategy": "VARCHAR(20) DEFAULT 'exponential'",
    "backoff_base_seconds": "NUMERIC(10,2) DEFAULT 1.0",
}

GOVERNANCE_COLUMNS = {
    "token_usage": "JSON",
    "token_budget": "INTEGER",
    "estimated_cost_usd": "NUMERIC(12,6)",
    "actual_cost_usd": "NUMERIC(12,6)",
    "cost_policy": "VARCHAR(100)",
}


class AddDurabilityAndGovernance(Migration):
    """Add durability fields and checkpoint table."""

    aliases = ["add_durability_and_governance"]
    description = (
        "Add durability columns (checkpoint, retry, backoff) to tasks table "
        "and create task_checkpoints table"
    )

    def upgrade(self, engine: Engine) -> None:
        """Apply migration."""
        self._add_durability_columns(engine)
        self._add_governance_columns(engine)
        self._create_checkpoints_table(engine)

    def _add_durability_columns(self, engine: Engine) -> None:
        """Add durability columns to the tasks table."""
        table_name = TASK_TABLE_NAME

        try:
            inspector = sa_inspect(engine)
            if table_name not in inspector.get_table_names():
                logger.debug(f"Table '{table_name}' does not exist, skipping")
                return
        except Exception as e:
            logger.debug(f"Could not check table existence: {e}, skipping")
            return

        try:
            existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        except Exception as e:
            logger.warning(f"Could not get columns for '{table_name}': {e}")
            return

        for col_name, col_type in DURABILITY_COLUMNS.items():
            if col_name not in existing_columns:
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")
                        )
                    logger.info(f"  {self.id}: Added column '{col_name}' to '{table_name}'")
                except Exception as e:
                    logger.error(f"  {self.id}: Failed to add column '{col_name}': {e}")
                    raise

    def _add_governance_columns(self, engine: Engine) -> None:
        """Add governance columns to the tasks table."""
        table_name = TASK_TABLE_NAME

        try:
            inspector = sa_inspect(engine)
            if table_name not in inspector.get_table_names():
                logger.debug(f"Table '{table_name}' does not exist, skipping")
                return
        except Exception as e:
            logger.debug(f"Could not check table existence: {e}, skipping")
            return

        try:
            existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        except Exception as e:
            logger.warning(f"Could not get columns for '{table_name}': {e}")
            return

        for col_name, col_type in GOVERNANCE_COLUMNS.items():
            if col_name not in existing_columns:
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")
                        )
                    logger.info(f"  {self.id}: Added column '{col_name}' to '{table_name}'")
                except Exception as e:
                    logger.error(f"  {self.id}: Failed to add column '{col_name}': {e}")
                    raise

    def _create_checkpoints_table(self, engine: Engine) -> None:
        """Create the task_checkpoints table."""
        try:
            inspector = sa_inspect(engine)
            if "task_checkpoints" in inspector.get_table_names():
                logger.debug("Table 'task_checkpoints' already exists, skipping creation")
                return
        except Exception:
            pass

        ddl = f"""CREATE TABLE IF NOT EXISTS task_checkpoints (
            id VARCHAR(255) PRIMARY KEY,
            task_id VARCHAR(255) NOT NULL REFERENCES {TASK_TABLE_NAME}(id) ON DELETE CASCADE,
            checkpoint_data TEXT NOT NULL,
            step_name VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""

        try:
            with engine.begin() as conn:
                conn.execute(text(ddl))
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_task_checkpoints_task_id "
                        "ON task_checkpoints(task_id)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_task_checkpoints_created_at "
                        "ON task_checkpoints(created_at)"
                    )
                )
            logger.info(f"  {self.id}: Created task_checkpoints table with indexes")
        except Exception as e:
            logger.error(f"  {self.id}: Failed to create task_checkpoints table: {e}")
            raise

    def downgrade(self, engine: Engine) -> None:
        """Rollback migration."""
        table_name = TASK_TABLE_NAME

        # Drop checkpoints table
        try:
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE IF EXISTS task_checkpoints"))
            logger.info(f"  Downgrade {self.id}: Dropped task_checkpoints table")
        except Exception as e:
            logger.warning(f"  Downgrade {self.id}: Could not drop task_checkpoints: {e}")

        # Drop durability columns
        try:
            inspector = sa_inspect(engine)
            if table_name not in inspector.get_table_names():
                return
            existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        except Exception:
            return

        for col_name in list(DURABILITY_COLUMNS) + list(GOVERNANCE_COLUMNS):
            if col_name in existing_columns:
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text(f"ALTER TABLE {table_name} DROP COLUMN {col_name}")
                        )
                    logger.info(f"  Downgrade {self.id}: Dropped column '{col_name}'")
                except Exception as e:
                    logger.warning(
                        f"  Downgrade {self.id}: Could not drop column '{col_name}': {e}"
                    )
