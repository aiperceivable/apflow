"""CLI metadata must not initialize storage before options are parsed."""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apflow.app import ApflowApp
from apflow.cli import _build_cli


@pytest.fixture
def management_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ApflowApp]:
    from apflow.app import create_app

    application = create_app(connection_string=f"sqlite:///{tmp_path / 'management.db'}")
    monkeypatch.setattr("apflow.app.create_app", lambda: application)
    monkeypatch.setattr(
        "apcore_cli.security.audit.AuditLogger.DEFAULT_PATH", tmp_path / "cli-audit.jsonl"
    )
    try:
        yield application
    finally:
        engine = application.session.get_bind().engine
        application.session.close()
        engine.dispose()


def test_run_cli_retry_is_idempotent_and_preserves_old_outcomes(
    management_app: ApflowApp,
) -> None:
    from apflow.core.execution.runs import RunService

    task = asyncio.run(
        management_app.task_repository.create_task(name="Failed CLI run", status="failed")
    )
    asyncio.run(RunService(management_app.session).ensure_run(task.id))
    command, runner = _build_cli(), CliRunner()
    before = runner.invoke(command, ["run", "get", "--run-id", task.id])
    assert before.exit_code == 0, before.output
    arguments = [
        "run",
        "retry",
        "--run-id",
        task.id,
        "--idempotency-key",
        "cli-retry-1",
        "--input",
        "-",
    ]
    selection = json.dumps({"task_ids": [task.id]})
    first = runner.invoke(command, arguments, input=selection)
    repeated = runner.invoke(command, arguments, input=selection)
    assert first.exit_code == repeated.exit_code == 0, first.output + repeated.output
    receipt = json.loads(first.output)
    assert json.loads(repeated.output) == receipt
    generic = runner.invoke(command, ["apflow", "run.retry", *arguments[2:]], input=selection)
    assert generic.exit_code == 0 and json.loads(generic.output) == receipt
    assert receipt["run_id"] != task.id
    assert receipt["retry_of_run_id"] == task.id
    new_run = runner.invoke(command, ["run", "get", "--run-id", receipt["run_id"]])
    assert new_run.exit_code == 0, new_run.output
    assert json.loads(new_run.output)["retry_of_run_id"] == task.id
    after = runner.invoke(command, ["run", "get", "--run-id", task.id])
    assert after.exit_code == 0, after.output
    assert json.loads(before.output) == json.loads(after.output)
    original = asyncio.run(management_app.task_repository.get_task_by_id(task.id))
    assert original is not None and original.status == "failed"


def test_budget_and_usage_cli_reconcile_once_and_acknowledge_idempotently(
    management_app: ApflowApp,
) -> None:
    from apflow.durability.attempts import AttemptRecorder
    from apflow.governance.ledger import BudgetLedger
    from apflow.governance.usage import UsageRecorder

    command, runner = _build_cli(), CliRunner()
    configured = runner.invoke(
        command, ["budget", "scope", "--scope", "cli-scope", "--limit-tokens", "100"]
    )
    assert configured.exit_code == 0, configured.output
    key = json.loads(configured.output)["scope_id"]
    task = asyncio.run(management_app.task_repository.create_task(name="Unknown CLI receipt"))
    recorder = AttemptRecorder(management_app.session)
    invocation = asyncio.run(recorder.start(task.id, 0, 1))
    ledger = BudgetLedger(management_app.session)
    asyncio.run(ledger.reserve(key, task.id, invocation, 10))
    asyncio.run(recorder.finish(invocation, "unknown"))
    arguments = [
        "budget",
        "reconcile",
        "--scope",
        "cli-scope",
        "--reservation-id",
        invocation,
        "--actual-tokens",
        "5",
        "--evidence-ref",
        "receipt:cli-confirmed",
    ]
    denied = runner.invoke(command, arguments, env={"APCORE_CLI_AUTO_APPROVE": "0"})
    assert denied.exit_code != 0
    assert asyncio.run(ledger.get_scope(key)).spent_tokens == 0
    for _ in range(2):
        reconciled = runner.invoke(command, arguments, env={"APCORE_CLI_AUTO_APPROVE": "1"})
        assert reconciled.exit_code == 0, reconciled.output
    page = runner.invoke(command, ["usage", "events", "--after-index", "0", "--limit", "10"])
    assert page.exit_code == 0, page.output
    events = json.loads(page.output)["events"]
    assert len(events) == 1 and events[0]["token_usage"]["total"] == 5
    for _ in range(2):
        acknowledged = runner.invoke(command, ["usage", "ack", "--event-id", events[0]["id"]])
        assert acknowledged.exit_code == 0, acknowledged.output
    stored = asyncio.run(UsageRecorder(management_app.session).list_events(None))
    assert len(stored) == 1 and stored[0]["delivered_at"] is not None
    assert asyncio.run(ledger.get_scope(key)).spent_tokens == 5


def test_cli_help_and_version_do_not_create_app() -> None:
    with patch("apflow.app.create_app", side_effect=AssertionError("unexpected storage")):
        command = _build_cli()
        for arguments in (
            ["--help"],
            ["--version"],
            ["rest", "--help"],
            ["run", "retry", "--help"],
            ["budget", "scope", "--help"],
            ["usage", "events", "--help"],
        ):
            result = CliRunner().invoke(command, arguments)
            assert result.exit_code == 0, result.output


def test_app_pool_uses_explicit_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apflow.app import create_app

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'other.db'}")
    app = create_app(connection_string=f"sqlite:///{tmp_path / 'chosen.db'}")
    try:
        with app.session_factory() as session:
            assert session.get_bind() is app.session.get_bind()
        assert not (tmp_path / "other.db").exists()
    finally:
        app.session.close()


def test_root_cli_explicit_database_never_initializes_environment_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apflow.app import create_app

    chosen = tmp_path / "cli-chosen.db"
    host = tmp_path / "host-application.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{host}")
    applications = []

    def record_application(**options: Any) -> ApflowApp:
        application = create_app(**options)
        applications.append(application)
        return application

    monkeypatch.setattr("apflow.app.create_app", record_application)
    monkeypatch.setattr("apflow.api.serve_rest", lambda *args, **kwargs: None)
    try:
        result = CliRunner().invoke(_build_cli(), ["rest", "--db", f"sqlite:///{chosen}"])
        assert result.exit_code == 0, result.output
        assert len(applications) == 1
        assert chosen.exists()
        assert not host.exists()
    finally:
        for application in applications:
            application.session.close()
