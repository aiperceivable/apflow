"""
Migration: Add a partial unique index on idempotency_key

Enforces effectively-once scheduled dispatch: a duplicate dispatch of the same
scheduled occurrence carries the same deterministic idempotency_key and is
rejected at INSERT. Only rows that set a key are constrained (partial index
WHERE idempotency_key IS NOT NULL), so regular tasks and manual triggers, which
leave it NULL, are unaffected. The same partial-unique syntax works on both
PostgreSQL and SQLite (>= 3.8.0).

File: 005_add_idempotency_key_unique_index.py
ID: 005_add_idempotency_key_unique_index (auto-extracted from filename)
"""

from sqlalchemy import Engine, text
from apflow.core.storage.migrations import Migration
from apflow.core.storage.sqlalchemy.models import TASK_TABLE_NAME
from apflow.logger import get_logger

logger = get_logger(__name__)


class AddIdempotencyKeyUniqueIndex(Migration):
    """Add a partial unique index on apflow_tasks.idempotency_key."""

    aliases = ["add_idempotency_key_unique_index"]
    description = (
        "Add partial unique index uq_{table}_idempotency_key "
        "(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )

    def _index_name(self) -> str:
        return f"uq_{TASK_TABLE_NAME}_idempotency_key"

    def upgrade(self, engine: Engine) -> None:
        table_name = TASK_TABLE_NAME
        index_name = self._index_name()
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                        f"ON {table_name} (idempotency_key) "
                        f"WHERE idempotency_key IS NOT NULL"
                    )
                )
            logger.info(f"✓ {self.id}: Created partial unique index '{index_name}'")
        except Exception as e:
            logger.error(f"✗ {self.id}: Failed to create index '{index_name}': {str(e)}")
            raise

    def downgrade(self, engine: Engine) -> None:
        index_name = self._index_name()
        try:
            with engine.begin() as conn:
                conn.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
            logger.info(f"✓ Downgrade {self.id}: Dropped index '{index_name}'")
        except Exception as e:
            logger.warning(f"⚠ Downgrade {self.id}: Could not drop index '{index_name}': {str(e)}")
