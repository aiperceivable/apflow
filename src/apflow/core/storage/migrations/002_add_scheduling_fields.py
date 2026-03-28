"""
Migration: Add scheduling fields to TaskModel

This migration adds scheduling-related fields to support cron-like task scheduling:
1. schedule_type - Type of schedule (once, interval, cron, daily, weekly, monthly)
2. schedule_expression - The schedule expression (format depends on type)
3. schedule_enabled - Whether scheduling is enabled
4. schedule_start_at - Earliest time the schedule can trigger
5. schedule_end_at - Latest time the schedule can trigger
6. next_run_at - Next scheduled execution time
7. last_run_at - Last time this scheduled task was executed
8. max_runs - Maximum number of scheduled runs
9. run_count - Number of times this scheduled task has been executed

File: 002_add_scheduling_fields.py
ID: 002_add_scheduling_fields (auto-extracted from filename)
"""

from sqlalchemy import Engine, inspect as sa_inspect, text
from apflow.core.storage.migrations import Migration
from apflow.core.storage.sqlalchemy.models import TASK_TABLE_NAME
from apflow.logger import get_logger

logger = get_logger(__name__)


class AddSchedulingFields(Migration):
    """Add scheduling fields to TaskModel"""

    aliases = ["add_scheduling_fields"]
    description = "Add scheduling fields: schedule_type, schedule_expression, schedule_enabled, schedule_start_at, schedule_end_at, next_run_at, last_run_at, max_runs, run_count"

    def upgrade(self, engine: Engine) -> None:
        """Apply migration"""
        table_name = TASK_TABLE_NAME

        # Check if table exists using SQLAlchemy inspector (works for SQLite + PostgreSQL)
        try:
            inspector = sa_inspect(engine)
            if table_name not in inspector.get_table_names():
                logger.debug(f"Table '{table_name}' does not exist, skipping migration")
                return
        except Exception as e:
            logger.debug(f"Could not check table existence: {str(e)}, skipping migration")
            return

        # Get existing columns using SQLAlchemy inspector
        try:
            existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        except Exception as e:
            logger.warning(f"Could not get columns for '{table_name}': {str(e)}")
            return

        # Define new columns to add
        new_columns = {
            # Scheduling configuration
            "schedule_type": "VARCHAR(20)",
            "schedule_expression": "VARCHAR(100)",
            "schedule_enabled": "BOOLEAN DEFAULT FALSE",
            # Schedule boundaries
            "schedule_start_at": "TIMESTAMP WITH TIME ZONE",
            "schedule_end_at": "TIMESTAMP WITH TIME ZONE",
            # Schedule state
            "next_run_at": "TIMESTAMP WITH TIME ZONE",
            "last_run_at": "TIMESTAMP WITH TIME ZONE",
            # Execution control
            "max_runs": "INTEGER",
            "run_count": "INTEGER DEFAULT 0",
        }

        # Add each column if it doesn't exist
        for col_name, col_type in new_columns.items():
            if col_name not in existing_columns:
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")
                        )
                    logger.info(f"✓ {self.id}: Added column '{col_name}' to '{table_name}'")
                    existing_columns.add(col_name)
                except Exception as e:
                    logger.error(f"✗ {self.id}: Failed to add column '{col_name}': {str(e)}")
                    raise

        # Create indexes for frequently queried columns
        indexes_to_create = [
            ("schedule_type", f"idx_{table_name}_schedule_type"),
            ("schedule_enabled", f"idx_{table_name}_schedule_enabled"),
            ("next_run_at", f"idx_{table_name}_next_run_at"),
        ]

        try:
            with engine.begin() as conn:
                for col_name, idx_name in indexes_to_create:
                    conn.execute(
                        text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table_name} ({col_name})")
                    )
            logger.info(f"✓ {self.id}: Created indexes for scheduling columns")
        except Exception as e:
            logger.warning(f"⚠ {self.id}: Could not create all indexes: {str(e)}")
            # Non-critical, continue

    def downgrade(self, engine: Engine) -> None:
        """Rollback migration (drop columns)"""
        table_name = TASK_TABLE_NAME

        # Check if table exists using SQLAlchemy inspector
        try:
            inspector = sa_inspect(engine)
            if table_name not in inspector.get_table_names():
                logger.debug(f"Table '{table_name}' does not exist, skipping downgrade")
                return
        except Exception as e:
            logger.debug(f"Could not check table existence: {str(e)}, skipping downgrade")
            return

        # Get existing columns
        try:
            existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        except Exception as e:
            logger.warning(f"Could not get columns for '{table_name}': {str(e)}")
            return

        # Columns to drop
        columns_to_drop = [
            "schedule_type",
            "schedule_expression",
            "schedule_enabled",
            "schedule_start_at",
            "schedule_end_at",
            "next_run_at",
            "last_run_at",
            "max_runs",
            "run_count",
        ]

        for col_name in columns_to_drop:
            if col_name in existing_columns:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {col_name}"))
                    logger.info(f"✓ Downgrade {self.id}: Dropped column '{col_name}'")
                except Exception as e:
                    logger.warning(
                        f"⚠ Downgrade {self.id}: Could not drop column '{col_name}': {str(e)}"
                    )

        logger.info(f"Downgrade {self.id}: Completed")
