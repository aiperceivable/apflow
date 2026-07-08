"""Integration tests for SQLite session creation and migrations"""

import os
import tempfile

import pytest
from sqlalchemy import text

from apflow.core.storage.factory import create_session, reset_default_session


@pytest.fixture(autouse=True)
def cleanup_session():
    """Reset default session between tests."""
    yield
    reset_default_session()


class TestSQLiteSessionCreation:
    def test_create_session_memory(self):
        """Create session with SQLite in-memory mode."""
        session = create_session(connection_string="sqlite:///:memory:")
        assert session is not None
        session.close()

    def test_create_session_default(self):
        """Default create_session uses SQLite."""
        session = create_session(path=":memory:")
        assert session is not None
        session.close()

    def test_create_session_file(self):
        """Create session with SQLite file."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            session = create_session(connection_string=f"sqlite:///{db_path}")
            assert session is not None
            session.close()
            assert os.path.exists(db_path)
        finally:
            os.unlink(db_path)

    def test_unsupported_connection_string_raises(self):
        """Unsupported connection string raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported connection string"):
            create_session(connection_string="mysql://localhost/test")


class TestSQLiteWALMode:
    def test_wal_mode_enabled_on_file(self):
        """File-based SQLite should have WAL mode enabled."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            session = create_session(connection_string=f"sqlite:///{db_path}")
            result = session.execute(text("PRAGMA journal_mode"))
            journal_mode = result.scalar()
            assert journal_mode == "wal"
            session.close()
        finally:
            # WAL creates additional files
            for ext in ["", "-wal", "-shm"]:
                path = db_path + ext
                if os.path.exists(path):
                    os.unlink(path)

    def test_foreign_keys_enabled(self):
        """SQLite should have foreign_keys=ON."""
        session = create_session(connection_string="sqlite:///:memory:")
        result = session.execute(text("PRAGMA foreign_keys"))
        assert result.scalar() == 1
        session.close()

    def test_busy_timeout_set(self):
        """SQLite should have busy_timeout configured."""
        session = create_session(connection_string="sqlite:///:memory:")
        result = session.execute(text("PRAGMA busy_timeout"))
        assert result.scalar() == 5000
        session.close()


class TestSQLiteTaskCRUD:
    def test_create_and_query_task(self):
        """Create a task and query it back using SQLite."""
        from apflow.core.storage.sqlalchemy.models import TaskModel

        session = create_session(connection_string="sqlite:///:memory:")

        task = TaskModel.create(
            {
                "name": "test_task",
                "status": "pending",
            }
        )
        session.add(task)
        session.commit()

        queried = session.query(TaskModel).filter_by(name="test_task").first()
        assert queried is not None
        assert queried.name == "test_task"
        assert queried.status == "pending"
        assert queried.id is not None

        session.close()


class TestSQLiteMigrations:
    def _create_fresh_session(self):
        """Create a fresh SQLite in-memory session with tables."""
        return create_session(connection_string="sqlite:///:memory:")

    def test_tables_created_on_session(self):
        """Tables should be created automatically on session creation."""
        from sqlalchemy import inspect as sa_inspect

        session = create_session(connection_string="sqlite:///:memory:")
        engine = session.get_bind()
        inspector = sa_inspect(engine)
        table_names = inspector.get_table_names()
        assert "apflow_tasks" in table_names
        session.close()

    def test_migrations_run_on_session_creation(self):
        """Regression: create_session() (used by create_app()) previously
        never ran schema migrations at all — only SessionPoolManager
        .initialize()'s sync branch did. (Review BLOCKER #2)"""
        from apflow.core.storage.migrate import MigrationHistoryTable

        session = create_session(connection_string="sqlite:///:memory:")
        engine = session.get_bind()
        applied = MigrationHistoryTable.get_applied(engine)
        assert len(applied) > 0, "No migrations recorded after create_session()"
        session.close()


class TestAsyncSessionMigrationBridge:
    """Regression: create_session()'s async-mode branch never invoked schema
    migrations for async (PostgreSQL) engines — MigrationManager can't run
    against an AsyncEngine, and no fallback existed. (Review BLOCKER #2)"""

    def test_create_session_async_postgres_calls_migration_bridge(self, monkeypatch):
        import apflow.core.storage.factory as factory_module

        calls = []
        monkeypatch.setattr(
            factory_module,
            "_migrate_schema_for_async_engine",
            lambda conn_str, dialect: calls.append((conn_str, dialect)),
        )

        create_session(
            connection_string="postgresql://user:pass@localhost:59999/nonexistent_db",
            async_mode=True,
        )

        assert len(calls) == 1
        _, dialect = calls[0]
        assert dialect == "postgresql"

    def test_migrate_schema_for_async_engine_builds_disposable_sync_engine(self, monkeypatch):
        import apflow.core.storage.factory as factory_module

        recorded = {}

        def fake_migrate(engine):
            recorded["engine"] = engine

        monkeypatch.setattr(factory_module, "_migrate_schema_if_needed", fake_migrate)

        disposed = []
        original_create_engine = factory_module.create_engine

        def tracking_create_engine(url, *args, **kwargs):
            engine = original_create_engine(url, *args, **kwargs)
            original_dispose = engine.dispose

            def tracking_dispose():
                disposed.append(True)
                original_dispose()

            engine.dispose = tracking_dispose
            return engine

        monkeypatch.setattr(factory_module, "create_engine", tracking_create_engine)

        factory_module._migrate_schema_for_async_engine(
            "postgresql+asyncpg://user:pass@localhost/db", "postgresql"
        )

        assert "engine" in recorded
        assert str(recorded["engine"].url).startswith("postgresql+psycopg2://")
        assert disposed == [True]

    def test_migrate_schema_for_async_engine_skips_non_postgres(self, monkeypatch):
        import apflow.core.storage.factory as factory_module

        called = []
        monkeypatch.setattr(
            factory_module, "_migrate_schema_if_needed", lambda engine: called.append(engine)
        )

        factory_module._migrate_schema_for_async_engine("sqlite+aiosqlite:///:memory:", "sqlite")

        assert called == []
