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
        self._dedup_idempotency_keys(engine, table_name)
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

    def _dedup_idempotency_keys(self, engine: Engine, table_name: str) -> None:
        """Clear idempotency_key on all but the earliest row for each duplicate.

        A pre-existing leader-handoff race (the exact race this index is meant
        to prevent going forward) can leave duplicate non-null idempotency_key
        values in the table from before this migration ever ran. Building the
        unique index against that data would hard-fail and block app boot, so
        null out the key on the later duplicates first — the rows themselves
        are untouched, only their now-ambiguous dedup key is cleared.
        """
        with engine.begin() as conn:
            duplicate_keys = (
                conn.execute(
                    text(
                        f"SELECT idempotency_key FROM {table_name} "
                        "WHERE idempotency_key IS NOT NULL "
                        "GROUP BY idempotency_key HAVING COUNT(*) > 1"
                    )
                )
                .scalars()
                .all()
            )
            if not duplicate_keys:
                return

            cleared = 0
            for key in duplicate_keys:
                row_ids = (
                    conn.execute(
                        text(
                            f"SELECT id FROM {table_name} WHERE idempotency_key = :key "
                            "ORDER BY created_at ASC"
                        ),
                        {"key": key},
                    )
                    .scalars()
                    .all()
                )
                for row_id in row_ids[1:]:
                    conn.execute(
                        text(f"UPDATE {table_name} SET idempotency_key = NULL WHERE id = :id"),
                        {"id": row_id},
                    )
                    cleared += 1

        logger.warning(
            f"⚠ {self.id}: Cleared idempotency_key on {cleared} duplicate row(s) "
            f"across {len(duplicate_keys)} key(s) before creating unique index"
        )

    def downgrade(self, engine: Engine) -> None:
        index_name = self._index_name()
        try:
            with engine.begin() as conn:
                conn.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
            logger.info(f"✓ Downgrade {self.id}: Dropped index '{index_name}'")
        except Exception as e:
            logger.warning(f"⚠ Downgrade {self.id}: Could not drop index '{index_name}': {str(e)}")
