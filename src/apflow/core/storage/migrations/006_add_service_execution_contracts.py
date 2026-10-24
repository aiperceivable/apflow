"""Persist cancellation requests, structured failures, and shared budget scopes."""

from typing import cast

from sqlalchemy import Engine, Table, inspect, text

from apflow.core.storage.migrations import Migration
from apflow.core.storage.sqlalchemy.models import (
    TASK_TABLE_NAME,
    BudgetAccount,
    BudgetReservation,
    ExecutionAttempt,
    SubmissionReceipt,
    TaskDispatch,
)


class AddServiceExecutionContracts(Migration):
    """Upgrade existing task rows without changing their execution semantics."""

    description = "Add service dispatch, receipts, cancellation, errors, and token reservations"

    def upgrade(self, engine: Engine) -> None:
        inspector = inspect(engine)
        if TASK_TABLE_NAME in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns(TASK_TABLE_NAME)}
            additions = {
                "cancel_requested_at": "TIMESTAMP WITH TIME ZONE",
                "error_info": "JSON",
                "budget_scope_id": "VARCHAR(255)",
                "execution_identity": "JSON",
            }
            with engine.begin() as connection:
                for name, sql_type in additions.items():
                    if name not in columns:
                        connection.execute(
                            text(f"ALTER TABLE {TASK_TABLE_NAME} ADD COLUMN {name} {sql_type}")
                        )
        for model in (
            SubmissionReceipt,
            TaskDispatch,
            BudgetAccount,
            BudgetReservation,
            ExecutionAttempt,
        ):
            cast(Table, model.__table__).create(engine, checkfirst=True)

    def downgrade(self, engine: Engine) -> None:
        """Retain financial and submission records rather than destroy evidence."""
        raise RuntimeError("Service execution records require an explicit archival migration")
