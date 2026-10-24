"""Isolated PostgreSQL resources shared by live integration acceptance tests."""

from collections.abc import Iterator
from contextlib import contextmanager
import uuid

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from apflow.core.storage.factory import create_session, normalize_postgresql_url


@contextmanager
def disposable_postgres_database(configured_url: str) -> Iterator[str]:
    """Create and remove only a randomly named database on the selected test server."""
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
