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
