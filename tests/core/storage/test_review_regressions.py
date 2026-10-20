"""Regression coverage for storage and scheduling review findings."""

import importlib
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session

from apflow.core.storage import factory
from apflow.core.storage.sqlalchemy.models import Base
from apflow.core.storage.sqlalchemy.schedule_calculator import ScheduleCalculator
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


@pytest.fixture
def storage_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


@pytest.mark.asyncio
async def test_increment_token_usage_refreshes_loaded_task(storage_session: Session) -> None:
    repository = TaskRepository(storage_session)
    task = await repository.create_task("Usage", token_usage={"input": 2, "output": 3, "total": 5})

    updated = await repository.increment_token_usage(task.id, 7, 11, 18)

    assert updated is task
    assert task.token_usage == {"input": 9, "output": 14, "total": 23}
    updated = await repository.increment_token_usage(task.id, 1, 1, 2)
    assert updated is task
    assert task.token_usage == {"input": 10, "output": 15, "total": 25}


@pytest.mark.asyncio
async def test_initialize_schedule_uses_loaded_sqlite_start_boundary(
    storage_session: Session,
) -> None:
    repository = TaskRepository(storage_session)
    start = datetime(2030, 1, 2, tzinfo=timezone.utc)
    task = await repository.create_task(
        "Scheduled", schedule_type="interval", schedule_expression="60", schedule_start_at=start
    )
    assert task.schedule_start_at is not None
    assert task.schedule_start_at.tzinfo is None

    updated = await repository.initialize_schedule(task.id, from_time=start - timedelta(days=1))

    assert updated is not None
    assert updated.next_run_at == (start + timedelta(seconds=60)).replace(tzinfo=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_complete_scheduled_run_checks_loaded_sqlite_end_boundary(
    storage_session: Session, expired: bool
) -> None:
    repository = TaskRepository(storage_session)
    end = datetime.now(timezone.utc) + timedelta(days=-1 if expired else 1)
    task = await repository.create_task(
        "Scheduled",
        schedule_type="interval",
        schedule_expression="60",
        schedule_enabled=True,
        schedule_end_at=end,
    )

    updated = await repository.complete_scheduled_run(task.id)

    assert updated is not None
    assert updated.run_count == 1
    assert updated.schedule_enabled is not expired
    assert (updated.next_run_at is None) is expired


@pytest.mark.parametrize(
    ("schedule_type", "expression"),
    [("daily", "09:00"), ("weekly", "1 09:00"), ("monthly", "1 09:00")],
)
@pytest.mark.parametrize(("month", "utc_hour"), [(1, 14), (7, 13)])
def test_calculate_schedule_converts_aware_reference_to_requested_timezone(
    schedule_type: str,
    expression: str,
    month: int,
    utc_hour: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "pytz", None)
    result = ScheduleCalculator.calculate_next_run(
        schedule_type,
        expression,
        datetime(2024, month, 1, tzinfo=timezone.utc),
        "America/New_York",
    )

    assert result == datetime(2024, month, 1, utc_hour, tzinfo=timezone.utc)


def test_current_schedule_time_uses_requested_timezone_without_pytz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "pytz", None)
    before = datetime.now(timezone.utc)

    result = ScheduleCalculator._get_current_time("America/New_York")

    assert before <= result <= datetime.now(timezone.utc)
    assert getattr(result.tzinfo, "key", None) == "America/New_York"


def test_localize_schedule_time_interprets_naive_reference_without_pytz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "pytz", None)

    result = ScheduleCalculator._localize_time(datetime(2024, 1, 1, 9), "America/New_York")

    assert result == datetime(2024, 1, 1, 14, tzinfo=timezone.utc)


@pytest.mark.parametrize("aware", [False, True])
def test_localize_schedule_time_preserves_invalid_timezone_fallback(aware: bool) -> None:
    reference = datetime(2024, 1, 1, tzinfo=timezone.utc if aware else None)

    result = ScheduleCalculator._localize_time(reference, "Invalid/Timezone")

    assert result == datetime(2024, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(("month", "day", "utc_hour"), [(3, 9, 13), (11, 2, 14)])
def test_daily_schedule_updates_utc_offset_across_daylight_saving(
    month: int, day: int, utc_hour: int
) -> None:
    result = ScheduleCalculator.calculate_next_run(
        "daily",
        "09:00",
        datetime(2024, month, day, 15, tzinfo=timezone.utc),
        "America/New_York",
    )

    assert result == datetime(2024, month, day + 1, utc_hour, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "reference",
    [
        datetime(2024, 3, 10, 5, 30, tzinfo=timezone.utc),
        datetime(2024, 11, 3, 4, 30, tzinfo=timezone.utc),
    ],
)
def test_interval_schedule_preserves_elapsed_seconds_across_daylight_saving(
    reference: datetime,
) -> None:
    result = ScheduleCalculator.calculate_next_run(
        "interval", "10800", reference, "America/New_York"
    )

    assert result == reference + timedelta(hours=3)


@pytest.mark.parametrize(
    "start_offset,end_offset,expected", [(1, 2, False), (-2, -1, False), (-1, 1, True)]
)
def test_schedule_validation_normalizes_sqlite_boundaries(
    start_offset: int, end_offset: int, expected: bool
) -> None:
    reference = datetime(2030, 1, 2, tzinfo=timezone.utc)

    result = ScheduleCalculator.is_schedule_valid(
        "interval",
        "60",
        schedule_start_at=(reference + timedelta(days=start_offset)).replace(tzinfo=None),
        schedule_end_at=(reference + timedelta(days=end_offset)).replace(tzinfo=None),
        from_time=reference,
    )

    assert result is expected


def test_create_session_propagates_migration_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = Mock(side_effect=RuntimeError("Migration failed"))
    monkeypatch.setattr(factory, "_migrate_schema_if_needed", migration)

    with pytest.raises(RuntimeError, match="Migration failed"):
        factory.create_session(path=":memory:")


def test_pool_failed_migration_clears_readiness_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = Mock(side_effect=[RuntimeError("Migration failed"), None])
    monkeypatch.setattr(factory, "_migrate_schema_if_needed", migration)
    manager = factory.SessionPoolManager()
    try:
        with pytest.raises(RuntimeError, match="Migration failed"):
            manager.initialize(path=":memory:")
        assert manager._engine is None
        assert manager._sessionmaker is None
        with pytest.raises(RuntimeError, match="not initialized"):
            manager.create_session()

        manager.initialize(path=":memory:")
        assert migration.call_count == 2
        session = manager.create_session()
        assert isinstance(session, Session)
        assert session.execute(text("SELECT 1")).scalar_one() == 1
        manager.release_session(session)
    finally:
        manager._dispose_current_engine()


@pytest.mark.asyncio
async def test_pooled_session_honors_explicit_database_after_default_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_path = tmp_path / "default.db"
    requested_path = tmp_path / "requested.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{default_path}")
    factory.reset_session_pool_manager()
    try:
        factory.get_session_pool_manager()
        async with factory.create_pooled_session(f"sqlite:///{requested_path}") as session:
            assert isinstance(session, Session)
            engine = session.get_bind()
            assert isinstance(engine, Engine)
            assert engine.url.database == str(requested_path)
    finally:
        factory.reset_session_pool_manager()


def test_tree_migration_backfills_legacy_schema_without_later_columns() -> None:
    migration = importlib.import_module(
        "apflow.core.storage.migrations.001_add_task_tree_fields"
    ).AddTaskTreeFields()
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE apflow_tasks (id VARCHAR(255) PRIMARY KEY, "
                    "parent_id VARCHAR(255), original_task_id VARCHAR(255), "
                    "created_at TIMESTAMP, has_copy BOOLEAN)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO apflow_tasks (id, parent_id, original_task_id, created_at) "
                    "VALUES ('root', NULL, NULL, '2024-01-01'), "
                    "('child', 'root', 'source', '2024-01-01'), "
                    "('grandchild', 'child', NULL, '2024-01-01')"
                )
            )

        migration.upgrade(engine)

        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT id, task_tree_id, origin_type FROM apflow_tasks ORDER BY id")
            ).all()
        assert rows == [
            ("child", "root", "copy"),
            ("grandchild", "root", None),
            ("root", "root", None),
        ]
    finally:
        engine.dispose()


def test_scheduling_migration_downgrade_drops_indexed_columns() -> None:
    migration = importlib.import_module(
        "apflow.core.storage.migrations.002_add_scheduling_fields"
    ).AddSchedulingFields()
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE apflow_tasks (id VARCHAR(255) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO apflow_tasks (id) VALUES ('preserved')"))
        migration.upgrade(engine)

        migration.downgrade(engine)

        assert [column["name"] for column in inspect(engine).get_columns("apflow_tasks")] == ["id"]
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT id FROM apflow_tasks")).scalar_one() == "preserved"
            )
    finally:
        engine.dispose()
