"""Execution failures must remain useful without retaining service credentials."""

import logging
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from apflow.core.base import BaseTask
from apflow.core.config_manager import get_config_manager
from apflow.core.execution.errors import ValidationError
from apflow.core.execution.error_info import execution_error_info
from apflow.core.execution.task_manager import TaskManager
from apflow.core.extensions import get_registry
from apflow.core.storage.sqlalchemy.models import TASK_TABLE_NAME, ExecutionAttempt
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


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
