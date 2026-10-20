"""
Migration: Add task_tree_id, origin_type, has_references fields

This migration:
1. Renames has_copy -> has_references (column rename)
2. Adds task_tree_id field for task tree grouping
3. Adds origin_type field for task origin tracking
4. Creates indexes for new columns

File: 001_add_task_tree_fields.py
ID: 001_add_task_tree_fields (auto-extracted from filename)
"""

from sqlalchemy import Engine, inspect as sa_inspect, text
from sqlalchemy.orm import Session
from apflow.core.storage.migrations import Migration
from apflow.core.storage.sqlalchemy.models import TASK_TABLE_NAME
from apflow.logger import get_logger

logger = get_logger(__name__)


class AddTaskTreeFields(Migration):
    """Add task_tree_id, origin_type, has_references fields"""

    aliases = ["add_task_tree_fields"]
    description = (
        "Add task_tree_id, origin_type, has_references. Rename has_copy to has_references."
    )

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

        # Step 1: Rename has_copy -> has_references
        if "has_copy" in existing_columns and "has_references" not in existing_columns:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(f"ALTER TABLE {table_name} RENAME COLUMN has_copy TO has_references")
                    )
                logger.info(
                    f"✓ {self.id}: Renamed column 'has_copy' to 'has_references' in '{table_name}'"
                )
                existing_columns.discard("has_copy")
                existing_columns.add("has_references")
            except Exception as e:
                error_msg = str(e)
                if "Dependency" in error_msg or "dependency" in error_msg:
                    logger.warning(
                        f"⚠ {self.id}: Could not rename 'has_copy' due to dependency; adding 'has_references' alongside and copying data"
                    )
                    try:
                        self._add_has_references_with_copy(engine, table_name)
                        existing_columns.add("has_references")
                    except Exception as fallback_err:
                        logger.error(
                            f"✗ {self.id}: Fallback add/copy for 'has_references' failed: {str(fallback_err)}"
                        )
                        raise
                else:
                    logger.error(
                        f"✗ {self.id}: Failed to rename column 'has_copy' to 'has_references': {error_msg}"
                    )
                    raise

        # Step 2: Add new columns
        new_columns = {
            "task_tree_id": "VARCHAR(255)",
            "origin_type": "VARCHAR(50)",
            "has_references": "BOOLEAN DEFAULT FALSE",
        }

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
                    logger.error(f"✗ v0.2.0: Failed to add column '{col_name}': {str(e)}")
                    raise

        # Step 3: Backfill origin_type for copies
        self._update_origin_types(engine)

        # Step 4: Auto-assign task_tree_id using only migration-era columns
        self._update_task_trees(engine)

        # Step 5: Create indexes
        indexes_to_create = [
            ("task_tree_id", f"idx_{table_name}_task_tree_id"),
            ("origin_type", f"idx_{table_name}_origin_type"),
            ("has_references", f"idx_{table_name}_has_references"),
        ]

        try:
            with engine.begin() as conn:
                for col_name, idx_name in indexes_to_create:
                    conn.execute(
                        text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table_name} ({col_name})")
                    )
            logger.info(f"✓ {self.id}: Created indexes for new columns")
        except Exception as e:
            logger.warning(f"⚠ {self.id}: Could not create all indexes: {str(e)}")
            # Non-critical, continue

    def _update_origin_types(self, engine: Engine) -> None:
        """Backfill copies without depending on columns added by later migrations."""
        with engine.begin() as connection:
            result = connection.execute(
                text(
                    f"UPDATE {TASK_TABLE_NAME} SET origin_type = 'copy' "
                    "WHERE origin_type IS NULL AND original_task_id IS NOT NULL "
                    "AND original_task_id != ''"
                )
            )
        if result.rowcount:
            logger.info(f"{self.id}: Backfilled origin_type='copy' for {result.rowcount} task(s)")

    def _update_task_trees(self, engine: Engine) -> None:
        """Assign each legacy root and its descendants to the same task tree."""
        batch_size = 100
        total_processed = 0
        with Session(engine) as session:
            while True:
                # Committed roots leave this filter, so OFFSET would skip rows.
                root_ids = (
                    session.execute(
                        text(
                            f"SELECT id FROM {TASK_TABLE_NAME} "
                            "WHERE task_tree_id IS NULL AND parent_id IS NULL "
                            "ORDER BY created_at ASC LIMIT :batch_size"
                        ),
                        {"batch_size": batch_size},
                    )
                    .scalars()
                    .all()
                )
                if not root_ids:
                    break
                for root_id in root_ids:
                    self._assign_task_tree_id_recursive(session, root_id, root_id)
                session.commit()
                total_processed += len(root_ids)
                logger.info(
                    f"{self.id}: Auto-assigned task_tree_id for {len(root_ids)} root task(s) "
                    f"(total: {total_processed})"
                )

    def _assign_task_tree_id_recursive(self, session: Session, task_id: str, tree_id: str) -> None:
        """Assign a tree ID using only columns present at this migration."""
        session.execute(
            text(
                f"UPDATE {TASK_TABLE_NAME} SET task_tree_id = :tree_id "
                "WHERE id = :task_id AND task_tree_id IS NULL"
            ),
            {"tree_id": tree_id, "task_id": task_id},
        )
        child_ids = (
            session.execute(
                text(
                    f"SELECT id FROM {TASK_TABLE_NAME} "
                    "WHERE parent_id = :task_id AND task_tree_id IS NULL"
                ),
                {"task_id": task_id},
            )
            .scalars()
            .all()
        )
        for child_id in child_ids:
            self._assign_task_tree_id_recursive(session, child_id, tree_id)

    def _add_has_references_with_copy(self, engine: Engine, table_name: str) -> None:
        """Fallback for databases that block column rename when dependencies exist.

        The caller only invokes this when it has already confirmed has_references
        does not exist yet, so a plain ADD COLUMN is sufficient — 'IF NOT EXISTS'
        is not valid syntax for ADD COLUMN on SQLite and crashes the migration.
        """
        with engine.begin() as conn:
            conn.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN has_references BOOLEAN DEFAULT FALSE")
            )
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE {table_name} SET has_references = has_copy WHERE has_copy IS NOT NULL"
                )
            )
        logger.info(
            f"✓ {self.id}: Added 'has_references' alongside 'has_copy' and copied existing values"
        )

    def downgrade(self, engine: Engine) -> None:
        """Rollback migration (drop new columns; restore has_copy if it was renamed away)"""
        table_name = TASK_TABLE_NAME
        inspector = sa_inspect(engine)

        if table_name not in inspector.get_table_names():
            logger.debug(f"Table '{table_name}' does not exist, skipping downgrade")
            return

        existing_columns = {col["name"] for col in inspector.get_columns(table_name)}

        # Restore has_copy before dropping has_references, so its data is not
        # lost. If has_copy already exists, upgrade() took the fallback
        # add-alongside path (has_copy already holds the original data) —
        # just drop the added has_references column below.
        if "has_references" in existing_columns and "has_copy" not in existing_columns:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(f"ALTER TABLE {table_name} RENAME COLUMN has_references TO has_copy")
                    )
                logger.info(
                    f"✓ Downgrade {self.id}: Renamed column 'has_references' back to 'has_copy'"
                )
                existing_columns.discard("has_references")
                existing_columns.add("has_copy")
            except Exception as e:
                logger.error(
                    f"✗ Downgrade {self.id}: Could not rename 'has_references' back to "
                    f"'has_copy': {str(e)}"
                )
                raise

        columns_to_drop = ["task_tree_id", "origin_type", "has_references"]

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
