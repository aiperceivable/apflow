"""PostgreSQL acceptance checks using disposable databases on an explicit test server."""

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
import importlib
import os
import uuid

import pytest
from sqlalchemy import DateTime, Engine, create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateColumn

from apflow.core.storage.factory import create_session, normalize_postgresql_url
from apflow.core.storage.sqlalchemy.models import TASK_TABLE_NAME, TaskCheckpointModel, TaskModel
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.durability.checkpoint import CheckpointManager

pytestmark = pytest.mark.integration


@pytest.fixture
def postgres_database_url() -> Iterator[str]:
    """Create and remove only a randomly named database on the explicitly selected server."""
    configured_url = os.getenv("APFLOW_TEST_POSTGRES_URL")
    if not configured_url:
        pytest.skip("APFLOW_TEST_POSTGRES_URL is required for live PostgreSQL acceptance tests")
    admin_url = make_url(normalize_postgresql_url(configured_url, async_mode=False))
    database_name = f"apflow_acceptance_{uuid.uuid4().hex}"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        quoted_name = admin_engine.dialect.identifier_preparer.quote(database_name)
        with admin_engine.connect() as connection:
            connection.execute(text(f"CREATE DATABASE {quoted_name}"))
        try:
            yield admin_url.set(database=database_name).render_as_string(hide_password=False)
        finally:
            with admin_engine.connect() as connection:
                connection.execute(text(f"DROP DATABASE {quoted_name}"))
    finally:
        admin_engine.dispose()


@contextmanager
def postgres_session(database_url: str) -> Iterator[Session]:
    """Open a public-factory session and dispose its pool before database cleanup."""
    session = create_session(connection_string=database_url, async_mode=False)
    assert isinstance(session, Session)
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    try:
        assert engine.dialect.name == "postgresql"
        yield session
    finally:
        session.close()
        engine.dispose()


def test_postgres_migration_004_upgrades_legacy_schema(postgres_database_url: str) -> None:
    migration_module = importlib.import_module(
        "apflow.core.storage.migrations.004_add_durability_and_governance"
    )
    migration = migration_module.AddDurabilityAndGovernance()
    new_columns = set(migration_module.DURABILITY_COLUMNS) | set(
        migration_module.GOVERNANCE_COLUMNS
    )
    engine = create_engine(postgres_database_url)
    try:
        definitions = [
            str(CreateColumn(column).compile(dialect=engine.dialect))
            for column in TaskModel.__mapper__.columns
            if column.name not in new_columns
        ]
        definitions.append("PRIMARY KEY (id)")
        with engine.begin() as connection:
            connection.execute(text(f"CREATE TABLE {TASK_TABLE_NAME} ({', '.join(definitions)})"))
            connection.execute(
                text(f"INSERT INTO {TASK_TABLE_NAME} (id, name) VALUES ('legacy', 'Legacy task')")
            )
        legacy_columns = {column["name"] for column in inspect(engine).get_columns(TASK_TABLE_NAME)}
        assert new_columns.isdisjoint(legacy_columns)

        migration.upgrade(engine)
        migration.upgrade(engine)

        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns(TASK_TABLE_NAME)}
        assert set(columns) == legacy_columns | new_columns
        checkpoint_type = columns["checkpoint_at"]["type"]
        assert isinstance(checkpoint_type, DateTime)
        assert checkpoint_type.timezone is True
        assert {column["name"] for column in inspector.get_columns("task_checkpoints")} == {
            "id",
            "task_id",
            "checkpoint_data",
            "step_name",
            "created_at",
        }
        checkpoint_indexes = {index["name"] for index in inspector.get_indexes("task_checkpoints")}
        assert {
            "idx_task_checkpoints_task_id",
            "idx_task_checkpoints_created_at",
        } <= checkpoint_indexes
        with Session(engine) as session:
            task = session.get(TaskModel, "legacy")
            assert task is not None
            assert task.name == "Legacy task"
            assert task.attempt_count == 0
            assert task.max_attempts == 3
            assert task.backoff_strategy == "exponential"
            assert task.backoff_base_seconds == Decimal("1.00")
            assert task.token_usage is None
            assert task.token_budget is None
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_postgres_checkpoint_survives_session_and_engine_reopen(
    postgres_database_url: str,
) -> None:
    checkpoint_data = {"position": 7, "completed": ["scan", "transform"], "nested": {"value": None}}
    with postgres_session(postgres_database_url) as session:
        task = await TaskRepository(session).create_task("Checkpointed task")
        task_id = task.id
        checkpoint_id = await CheckpointManager(session).save_checkpoint(
            task_id, checkpoint_data, step_name="transform"
        )

    with postgres_session(postgres_database_url) as reopened_session:
        assert await CheckpointManager(reopened_session).load_checkpoint(task_id) == checkpoint_data
        restored_task = reopened_session.get(TaskModel, task_id)
        assert restored_task is not None
        assert restored_task.resume_from == checkpoint_id
        assert restored_task.checkpoint_at is not None
        assert restored_task.checkpoint_at.tzinfo is not None
        checkpoint = reopened_session.scalar(
            select(TaskCheckpointModel).where(TaskCheckpointModel.id == checkpoint_id)
        )
        assert checkpoint is not None
        assert checkpoint.step_name == "transform"
        assert checkpoint.task_id == task_id


@pytest.mark.asyncio
async def test_postgres_factory_and_all_governance_fields_survive_reload(
    postgres_database_url: str,
) -> None:
    with postgres_session(postgres_database_url) as session:
        repository = TaskRepository(session)
        task = await repository.create_task(
            "Governed task",
            token_usage={"input": 10, "output": 5, "total": 15},
            token_budget=1000,
            estimated_cost_usd=Decimal("0.125000"),
            actual_cost_usd=Decimal("0.062500"),
            cost_policy="notify-at-eighty",
        )
        task_id = task.id
        updated = await repository.increment_token_usage(task_id, 7, 3, 10)
        assert updated is task
        assert task.token_usage == {"input": 17, "output": 8, "total": 25}
        assert (
            session.scalar(text("SELECT current_database()"))
            == make_url(postgres_database_url).database
        )

    with postgres_session(postgres_database_url) as reopened_session:
        restored = reopened_session.get(TaskModel, task_id)
        assert restored is not None
        assert restored.name == "Governed task"
        assert restored.token_usage == {"input": 17, "output": 8, "total": 25}
        assert restored.token_budget == 1000
        assert restored.estimated_cost_usd == Decimal("0.125000")
        assert restored.actual_cost_usd == Decimal("0.062500")
        assert restored.cost_policy == "notify-at-eighty"
