"""Add explicit runs and idempotent consumption facts without altering history."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import cast

from sqlalchemy import Engine, Table, inspect, select, text

from apflow.core.storage.migrations import Migration
from apflow.core.storage.sqlalchemy.models import (
    TASK_TABLE_NAME,
    ExecutionAttempt,
    TaskModel,
    UsageEvent,
    WorkflowRun,
)


class AddWorkflowRunsUsageEvents(Migration):
    """Add nullable lineage and preserve recognizable legacy usage as baseline facts."""

    description = "Add workflow runs, direct lineage, and immutable usage events"

    def upgrade(self, engine: Engine) -> None:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        additions = {
            TASK_TABLE_NAME: {
                "run_id": "VARCHAR(255)",
                "source_task_id": "VARCHAR(255)",
                "result_snapshot_hash": "VARCHAR(64)",
            },
            ExecutionAttempt.__tablename__: {"run_id": "VARCHAR(255)"},
        }
        with engine.begin() as connection:
            for table, fields in additions.items():
                if table not in tables:
                    continue
                columns = {column["name"] for column in inspector.get_columns(table)}
                for name, sql_type in fields.items():
                    if name not in columns:
                        connection.execute(
                            text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                        )
                    connection.execute(
                        text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{name} ON {table} ({name})")
                    )
        for model in (WorkflowRun, UsageEvent):
            cast(Table, model.__table__).create(engine, checkfirst=True)
        if TASK_TABLE_NAME in tables:
            self._preserve_legacy_usage(engine)

    def _preserve_legacy_usage(self, engine: Engine) -> None:
        """Inherited Copy/Link totals are not proof of another actual consumption."""
        with engine.begin() as connection:
            rows = connection.execute(select(TaskModel.__table__)).mappings()
            for row in rows:
                usage = row["token_usage"]
                if (
                    not isinstance(usage, dict)
                    or not usage
                    or row["origin_type"] in {"link", "copy"}
                ):
                    continue
                event_id = "legacy:" + sha256(str(row["id"]).encode()).hexdigest()
                if connection.execute(
                    select(UsageEvent.id).where(UsageEvent.id == event_id)
                ).first():
                    continue
                has_events = connection.execute(
                    select(UsageEvent.id).where(UsageEvent.task_id == row["id"]).limit(1)
                ).first()
                if has_events:
                    continue
                digest = sha256(
                    json.dumps([event_id, "legacy", usage], sort_keys=True).encode()
                ).hexdigest()
                connection.execute(
                    cast(Table, UsageEvent.__table__)
                    .insert()
                    .values(
                        id=event_id,
                        invocation_id=event_id,
                        task_id=row["id"],
                        run_id=row["run_id"],
                        owner_id=row["user_id"],
                        kind="legacy",
                        digest=digest,
                        reported_usage=usage,
                        token_usage=usage,
                        created_at=row["completed_at"]
                        or row["created_at"]
                        or datetime.now(timezone.utc),
                    )
                )

    def downgrade(self, engine: Engine) -> None:
        raise RuntimeError("Run and consumption history require an explicit archival migration")
