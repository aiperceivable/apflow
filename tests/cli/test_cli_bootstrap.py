"""CLI bootstrap guards for distributed/cluster mode.

These cover the fail-loud behavior added so that misconfigured cluster usage
errors clearly instead of silently degrading (serve --cluster used to construct
a runtime it never started; worker accepted non-PostgreSQL backends).
"""

from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apflow.cli import serve, worker


def test_serve_cluster_fails_loud() -> None:
    """`serve --cluster` must error and point operators at `apflow worker`."""
    result = CliRunner().invoke(serve, ["--cluster"])

    assert result.exit_code != 0
    assert "apflow worker" in result.output


def test_worker_rejects_non_postgres_db(monkeypatch) -> None:
    """`worker` must reject a non-PostgreSQL --db before starting the runtime."""
    engine = create_engine("sqlite:///:memory:")
    session = sessionmaker(bind=engine)()
    fake_app = SimpleNamespace(session=session)

    # Avoid the heavy full-stack bootstrap; isolate the backend guard.
    monkeypatch.setattr("apflow.app.create_app", lambda **_kwargs: fake_app)

    try:
        result = CliRunner().invoke(worker, ["--db", "sqlite:///:memory:"])
    finally:
        session.close()

    assert result.exit_code != 0
    assert "PostgreSQL" in result.output
