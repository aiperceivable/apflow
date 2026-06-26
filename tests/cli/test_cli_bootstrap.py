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

from apflow.cli import mcp, rest, serve, worker


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


def test_serve_scheduler_requires_all() -> None:
    """`serve --scheduler` without --all must error (needs the unified event loop)."""
    result = CliRunner().invoke(serve, ["--scheduler"])
    assert result.exit_code != 0
    assert "--all" in result.output


def test_rest_auth_requires_jwt_secret(monkeypatch) -> None:
    """`rest --auth` must fail early when no JWT secret is configured."""
    monkeypatch.setattr(
        "apflow.core.config_manager.get_config_manager",
        lambda: SimpleNamespace(
            jwt_secret=None,
            jwt_algorithm="HS256",
            jwt_public_key=None,
            jwt_public_key_path=None,
        ),
    )
    result = CliRunner().invoke(rest, ["--auth"])
    assert result.exit_code != 0
    assert "jwt" in result.output.lower() or "JWT" in result.output


def test_serve_auth_requires_jwt_secret(monkeypatch) -> None:
    """`serve --auth` must fail early when no JWT secret is configured."""
    monkeypatch.setattr(
        "apflow.core.config_manager.get_config_manager",
        lambda: SimpleNamespace(
            jwt_secret=None,
            jwt_algorithm="HS256",
            jwt_public_key=None,
            jwt_public_key_path=None,
        ),
    )
    result = CliRunner().invoke(serve, ["--auth"])
    assert result.exit_code != 0
    assert "secret" in result.output.lower()


def test_mcp_auth_requires_jwt_secret(monkeypatch) -> None:
    """`mcp --auth` must fail early when no JWT secret is configured."""
    monkeypatch.setattr(
        "apflow.core.config_manager.get_config_manager",
        lambda: SimpleNamespace(
            jwt_secret=None,
            jwt_algorithm="HS256",
            jwt_public_key=None,
            jwt_public_key_path=None,
        ),
    )
    result = CliRunner().invoke(mcp, ["--auth"])
    assert result.exit_code != 0
    assert "secret" in result.output.lower()


def _configured_jwt():
    """get_config_manager stub with a usable HS256 secret (jwt guard passes)."""
    return SimpleNamespace(
        jwt_secret="a-configured-secret",
        jwt_algorithm="HS256",
        jwt_public_key=None,
        jwt_public_key_path=None,
    )


def test_rest_webhook_under_auth_requires_secret(monkeypatch) -> None:
    """`rest --webhook --auth` without --webhook-secret must fail (else open endpoint)."""
    monkeypatch.setattr("apflow.core.config_manager.get_config_manager", lambda: _configured_jwt())
    result = CliRunner().invoke(rest, ["--auth", "--webhook"])
    assert result.exit_code != 0
    assert "webhook-secret" in result.output


def test_serve_all_webhook_under_auth_requires_secret(monkeypatch) -> None:
    """`serve --all --webhook --auth` without --webhook-secret must fail too."""
    monkeypatch.setattr("apflow.core.config_manager.get_config_manager", lambda: _configured_jwt())
    result = CliRunner().invoke(serve, ["--all", "--auth", "--webhook"])
    assert result.exit_code != 0
    assert "webhook-secret" in result.output


def test_rest_webhook_with_secret_under_auth_is_allowed(monkeypatch) -> None:
    """--webhook + --auth + --webhook-secret passes the guard (reaches create_app)."""
    monkeypatch.setattr("apflow.core.config_manager.get_config_manager", lambda: _configured_jwt())
    # create_app would start a server; stub it so the guard is what we exercise.
    monkeypatch.setattr("apflow.app.create_app", lambda **_kw: _FakeApp())
    monkeypatch.setattr("apflow.api.serve_rest", lambda *_a, **_k: None)
    result = CliRunner().invoke(rest, ["--auth", "--webhook", "--webhook-secret", "s3cret"])
    assert result.exit_code == 0, result.output


class _FakeApp:
    registry = SimpleNamespace(count=0)
