"""Durable service metadata survives reopening the database."""

from pathlib import Path

from importlib import import_module

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from apflow.core.storage.sqlalchemy.models import (
    Base,
    BudgetAccount,
    BudgetReservation,
    SubmissionReceipt,
    TaskDispatch,
    TaskModel,
)


def test_service_models_persist_receipts_dispatch_and_budget(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'service.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                SubmissionReceipt(scope="alice", key="create-1", digest="abc", receipt={"id": "t"}),
                TaskDispatch(task_id="t", identity={"subject": "alice"}),
                BudgetAccount(scope_id="experiment", limit_tokens=100),
                BudgetReservation(
                    id="attempt-1", scope_id="experiment", task_id="t", reserved_tokens=60
                ),
            ]
        )
        session.commit()
    with Session(engine) as session:
        receipt = session.get(SubmissionReceipt, ("alice", "create-1"))
        dispatch = session.get(TaskDispatch, "t")
        account = session.get(BudgetAccount, "experiment")
        reservation = session.get(BudgetReservation, "attempt-1")
        assert receipt is not None and receipt.receipt == {"id": "t"}
        assert dispatch is not None and dispatch.state == "accepted"
        assert account is not None and account.spent_tokens == 0
        assert reservation is not None and reservation.state == "reserved"
    engine.dispose()


def test_service_migration_upgrades_existing_table_idempotently() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE apflow_tasks (id VARCHAR(255) PRIMARY KEY)"))
    migration = import_module(
        "apflow.core.storage.migrations.006_add_service_execution_contracts"
    ).AddServiceExecutionContracts()
    migration.upgrade(engine)
    migration.upgrade(engine)
    columns = {column["name"] for column in inspect(engine).get_columns(TaskModel.__tablename__)}
    assert {"cancel_requested_at", "error_info", "budget_scope_id", "execution_identity"} <= columns
    assert {"apflow_submission_receipts", "apflow_task_dispatch", "apflow_budget_accounts"} <= set(
        inspect(engine).get_table_names()
    )
    engine.dispose()


def test_execution_identity_is_private_and_copied_only_for_same_owner() -> None:
    identity = {"id": "alice", "type": "user", "roles": ["reader"], "attrs": {}}
    task = TaskModel.create(
        {"id": "t", "name": "t", "user_id": "alice", "execution_identity": identity}
    )
    assert task.execution_identity == identity
    assert "execution_identity" not in task.to_dict()
    assert "execution_identity" not in task.output()
    copied = task.copy({"id": "copy"})
    assert copied.execution_identity is not None
    assert task.execution_identity is not None
    assert copied.execution_identity == identity
    copied.execution_identity["roles"].append("other")
    assert task.execution_identity["roles"] == ["reader"]
    reassigned = task.copy({"id": "reassigned", "user_id": "bob"})
    assert reassigned.execution_identity is None
