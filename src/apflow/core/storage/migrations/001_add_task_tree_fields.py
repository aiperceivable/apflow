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
from apflow.core.storage.sqlalchemy.models import TASK_TABLE_NAME, TaskModel
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

        # Step 4: Auto-assign task_tree_id for existing tasks (ORM-based, DB-agnostic)
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
        # After assigning task_tree_id, backfill origin_type for copies globally
        try:
            # Create a session from the engine
            from sqlalchemy.orm import sessionmaker

            SessionLocal = sessionmaker(bind=engine)
            session = SessionLocal()
            updated = (
                session.query(TaskModel)
                .filter(
                    TaskModel.origin_type.is_(None),
                    TaskModel.original_task_id.isnot(None),
                    TaskModel.original_task_id != "",
                )
                .update({TaskModel.origin_type: "copy"}, synchronize_session=False)
            )
            session.commit()
            if updated:
                logger.info(
                    f"✓ {self.id}: Backfilled origin_type='copy' for {updated} task(s) with original_task_id"
                )
        except Exception as backfill_err:
            session.rollback()
            logger.warning(
                f"⚠ {self.id}: Could not backfill origin_type for copies: {str(backfill_err)}"
            )

        finally:
            session.close()

    def _update_task_trees(self, engine: Engine) -> None:
        batch_size = 100
        offset = 0

        try:
            # Create a session from the engine
            from sqlalchemy.orm import sessionmaker

            SessionLocal = sessionmaker(bind=engine)
            session = SessionLocal()

            while True:
                try:
                    # Get batch of root tasks using ORM
                    root_tasks = (
                        session.query(TaskModel)
                        .filter(TaskModel.task_tree_id.is_(None), TaskModel.parent_id.is_(None))
                        .order_by(TaskModel.created_at.asc())
                        .limit(batch_size)
                        .offset(offset)
                        .all()
                    )

                    if not root_tasks:
                        break  # No more root tasks

                    # Process each root task and its descendants
                    for root_task in root_tasks:
                        self._assign_task_tree_id_recursive(session, root_task.id, root_task.id)

                    # Commit batch
                    session.commit()

                    logger.info(
                        f"✓ {self.id}: Auto-assigned task_tree_id for {len(root_tasks)} root task(s) "
                        f"(offset: {offset})"
                    )
                    offset += batch_size

                except Exception as batch_err:
                    session.rollback()
                    logger.warning(
                        f"⚠ {self.id}: Error in batch {offset // batch_size}: {str(batch_err)}"
                    )
                    raise

        except Exception as e:
            logger.warning(f"⚠ {self.id}: Could not auto-assign task_tree_id: {str(e)}")
            # Non-critical, continue
        finally:
            session.close()

    def _assign_task_tree_id_recursive(self, session: Session, task_id: str, tree_id: str) -> None:
        """
        Recursively assign task_tree_id to a task and all its descendants

        Args:
            session: SQLAlchemy session
            task_id: ID of current task to update
            tree_id: ID of root task (tree identifier)
        """
        # Update current task
        task = session.query(TaskModel).filter(TaskModel.id == task_id).first()
        if task and task.task_tree_id is None:
            task.task_tree_id = tree_id
            session.flush()  # Flush but don't commit yet

        # Recursively update all children
        children = (
            session.query(TaskModel)
            .filter(TaskModel.parent_id == task_id, TaskModel.task_tree_id.is_(None))
            .all()
        )

        for child in children:
            self._assign_task_tree_id_recursive(session, child.id, tree_id)

    def _add_has_references_with_copy(self, engine: Engine, table_name: str) -> None:
        """Fallback for databases that block column rename when dependencies exist."""
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS has_references BOOLEAN DEFAULT FALSE"
                )
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
        """Rollback migration (drop columns)"""
        table_name = TASK_TABLE_NAME
        inspector = sa_inspect(engine)

        if table_name not in inspector.get_table_names():
            logger.debug(f"Table '{table_name}' does not exist, skipping downgrade")
            return

        existing_columns = {col["name"] for col in inspector.get_columns(table_name)}

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

        # Rename has_references back to has_copy if needed
        if "has_references" not in existing_columns and "has_copy" not in existing_columns:
            # This is a fallback - in reality, the column was dropped above
            logger.info(f"Downgrade {self.id}: Completed")
