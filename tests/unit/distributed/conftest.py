"""Shared fixtures for distributed service tests."""

import os
from collections.abc import Iterator

import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apflow.core.storage.sqlalchemy.models import (
    Base,
    TaskModel,
    DistributedNode,
)
from apflow.core.distributed.config import DistributedConfig
from apflow.core.storage.factory import is_postgresql_url, normalize_postgresql_url


def _get_test_database_url() -> str | None:
    """Get test database URL from environment variable.

    Checks APFLOW_TEST_DATABASE_URL first, then TEST_DATABASE_URL,
    consistent with the root conftest convention.
    """
    return os.getenv("APFLOW_TEST_DATABASE_URL") or os.getenv("TEST_DATABASE_URL")


@pytest.fixture
def engine():
    """Create a database engine with all tables.

    Uses TEST_DATABASE_URL (or APFLOW_TEST_DATABASE_URL) if set (supports
    PostgreSQL); otherwise falls back to SQLite in-memory with StaticPool.
    """
    test_url = _get_test_database_url()

    if test_url and is_postgresql_url(test_url):
        url = normalize_postgresql_url(test_url, async_mode=False)
        eng = create_engine(url)
        Base.metadata.drop_all(eng)
        Base.metadata.create_all(eng)
        yield eng
        Base.metadata.drop_all(eng)
        eng.dispose()
    else:
        eng = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(eng)
        yield eng
        eng.dispose()


@pytest.fixture
def session_factory(engine) -> sessionmaker:
    """Provide a SQLAlchemy session factory."""
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    return factory


@pytest.fixture
def session(session_factory) -> Iterator[Session]:
    """Provide a single session for tests."""
    sess = session_factory()
    yield sess
    sess.close()


@pytest.fixture
def config() -> DistributedConfig:
    """Standard test config."""
    return DistributedConfig(
        enabled=True,
        node_id="test-leader",
        node_role="leader",
        heartbeat_interval_seconds=10,
        node_stale_threshold_seconds=30,
        node_dead_threshold_seconds=120,
        lease_duration_seconds=30,
    )


@pytest.fixture
def sample_node(session) -> DistributedNode:
    """A healthy node persisted to DB."""
    node = DistributedNode(
        node_id="worker-1",
        executor_types=["a2a"],
        capabilities={"gpu": False},
        status="healthy",
        heartbeat_at=datetime.now(timezone.utc),
    )
    session.add(node)
    session.commit()
    return node


@pytest.fixture
def sample_task(session) -> TaskModel:
    """A pending task persisted to DB."""
    task = TaskModel(id="task-1", name="test-task", status="pending")
    session.add(task)
    session.commit()
    return task
