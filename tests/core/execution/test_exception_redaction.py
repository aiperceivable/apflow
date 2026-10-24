"""Execution failures must remain useful without retaining service credentials."""

import json
import logging
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from apflow.core.base import BaseTask
from apflow.core.config_manager import get_config_manager
from apflow.core.execution.errors import ValidationError
from apflow.core.execution.error_info import execution_error_info, http_error_info
from apflow.core.execution.task_manager import TaskManager
from apflow.core.extensions import get_registry
from apflow.core.storage.sqlalchemy.models import TASK_TABLE_NAME, ExecutionAttempt
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.durability.retry import RetryManager


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ValidationError, RuntimeError])
@pytest.mark.parametrize("boundary", ["database", "logs"])
async def test_executor_failure_redacts_configured_credentials(
    sync_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
    boundary: str,
) -> None:
    secret = "synthetic-exception-credential"
    monkeypatch.setenv("APFLOW_TEST_ERROR_SECRET", secret)
    configuration = get_config_manager()
    monkeypatch.setattr(configuration, "_data", dict(configuration._data))
    configuration.set(
        "security.credentials", [{"id": "test", "env_var": "APFLOW_TEST_ERROR_SECRET"}]
    )

    class FailingExecutor(BaseTask):
        id = "exception_redaction_probe"
        name = "Failure probe"
        description = "Raise a credential-bearing error with a chained cause."
        result_format = "envelope"

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            try:
                raise ValueError(f"Upstream cause: {secret}")
            except ValueError as cause:
                error = error_type(f"Upstream failure: {secret}")
                info = execution_error_info(error)
                info["request_id"] = secret
                setattr(error, "error_info", info)
                raise error from cause

    registry = get_registry()
    registry.register(FailingExecutor(), executor_class=FailingExecutor)
    logger = logging.getLogger("apflow.core.execution.task_manager")
    logger.addHandler(caplog.handler)
    try:
        repository = TaskRepository(sync_db_session)
        task = await repository.create_task(
            name="Failure", params={"executor_id": FailingExecutor.id}
        )
        await TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])._execute_single_task(task)
        stored = await repository.get_task_by_id(task.id)
        assert stored is not None and stored.status == "failed"
        if boundary == "database":
            raw = sync_db_session.execute(
                text(f"SELECT error, error_info FROM {TASK_TABLE_NAME} WHERE id = :id"),
                {"id": task.id},
            ).one()
            assert "Upstream failure" in raw.error
            assert secret not in str(tuple(raw))
            history = sync_db_session.execute(
                text(
                    f"SELECT error_info FROM {ExecutionAttempt.__tablename__} WHERE task_id = :id"
                ),
                {"id": task.id},
            ).all()
            assert history and secret not in str(history)
        else:
            rendered = "\n".join(logging.Formatter().format(record) for record in caplog.records)
            assert "Upstream failure" in rendered
            assert secret not in rendered
            if error_type is RuntimeError:
                assert "Traceback" in rendered and "Upstream cause" in rendered
    finally:
        logger.removeHandler(caplog.handler)
        registry.unregister(FailingExecutor.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("representation", ["structured", "repr", "json", "ascii_json"])
async def test_executor_failure_redacts_escaped_credentials(
    sync_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    representation: str,
) -> None:
    secret = "synthetic\\páss'word\"\nend"
    monkeypatch.setenv("APFLOW_TEST_ERROR_SECRET", secret)
    configuration = get_config_manager()
    monkeypatch.setattr(configuration, "_data", dict(configuration._data))
    configuration.set(
        "security.credentials", [{"id": "test", "env_var": "APFLOW_TEST_ERROR_SECRET"}]
    )

    class FailingExecutor(BaseTask):
        id = "escaped_exception_probe"
        name = "Escaped failure probe"
        description = "Raise structured or serialized upstream diagnostics."

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            if representation == "structured":
                raise ValidationError(
                    "Failure",
                    what="Upstream rejected request",
                    context={"response": {"credential": secret}},
                )
            payload = (
                repr(secret)
                if representation == "repr"
                else json.dumps({"credential": secret}, ensure_ascii=representation == "ascii_json")
            )
            raise RuntimeError("Upstream rejected request: " + payload)

    registry = get_registry()
    registry.register(FailingExecutor(), executor_class=FailingExecutor)
    logger = logging.getLogger("apflow.core.execution.task_manager")
    logger.addHandler(caplog.handler)
    try:
        repository = TaskRepository(sync_db_session)
        task = await repository.create_task(
            name="Escaped failure", params={"executor_id": FailingExecutor.id}
        )
        await TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])._execute_single_task(task)
        raw_error = sync_db_session.execute(
            text(f"SELECT error FROM {TASK_TABLE_NAME} WHERE id = :id"), {"id": task.id}
        ).scalar_one()
        rendered = "\n".join(logging.Formatter().format(record) for record in caplog.records)
        for diagnostic in (raw_error, rendered):
            assert "Upstream rejected request" in diagnostic
            assert "***REDACTED***" in diagnostic
            for encoded in (
                secret,
                repr(secret)[1:-1],
                json.dumps(secret)[1:-1],
                json.dumps(secret, ensure_ascii=False)[1:-1],
            ):
                assert encoded not in diagnostic
    finally:
        logger.removeHandler(caplog.handler)
        registry.unregister(FailingExecutor.id)


@pytest.mark.asyncio
async def test_error_metadata_redaction_preserves_retry_and_usage(
    sync_db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "test-key"
    monkeypatch.setenv("APFLOW_TEST_ERROR_SECRET", secret)
    configuration = get_config_manager()
    monkeypatch.setattr(configuration, "_data", dict(configuration._data))
    configuration.set(
        "security.credentials", [{"id": "test", "env_var": "APFLOW_TEST_ERROR_SECRET"}]
    )
    calls: list[int] = []

    class RetryableExecutor(BaseTask):
        id = "bounded_metadata_probe"
        name = "Bounded metadata probe"
        description = "Return retryable diagnostics and authoritative usage."
        result_format = "envelope"

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            calls.append(1)
            return {
                "result": None,
                "error": "Upstream unavailable",
                "error_info": http_error_info(503, "GET", request_id="x" * 247 + secret),
                "token_usage": {"input": 2, "output": 3, "total": 5},
            }

    registry = get_registry()
    registry.register(RetryableExecutor(), executor_class=RetryableExecutor)
    try:
        repository = TaskRepository(sync_db_session)
        task = await repository.create_task(
            name="Metadata retry",
            params={"executor_id": RetryableExecutor.id},
            max_attempts=2,
            backoff_base_seconds=0.001,
        )
        await TaskManager(
            sync_db_session, pre_hooks=[], post_hooks=[], retry_manager=RetryManager()
        )._execute_single_task(task)
        stored = await repository.get_task_by_id(task.id)
        history = list(
            sync_db_session.scalars(
                select(ExecutionAttempt).where(ExecutionAttempt.task_id == task.id)
            )
        )
        assert len(calls) == 2
        assert stored is not None and stored.status == "failed"
        assert stored.error == "Upstream unavailable"
        assert stored.token_usage == {"input": 4, "output": 6, "total": 10}
        assert len(history) == 2
        assert all(row.status == "failed" and row.completed_at is not None for row in history)
        assert all(row.token_usage == {"input": 2, "output": 3, "total": 5} for row in history)
        assert history[0].next_retry_at is not None
        assert secret not in str([row.error_info for row in history])
    finally:
        registry.unregister(RetryableExecutor.id)
