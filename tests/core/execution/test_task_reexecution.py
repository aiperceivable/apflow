"""Repeated execution creates a new run while preserving the original history."""

from typing import Any

import pytest
from apcore.errors import ModuleError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from apflow.core.execution.run_retry import retry_run
from apflow.core.execution.runs import RunService
from apflow.core.execution.task_executor import TaskExecutor
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


@pytest.mark.asyncio
async def test_reexecute_failed_task_creates_new_history(
    use_test_db_session: Session | AsyncSession,
) -> None:
    repository = TaskRepository(use_test_db_session)
    task = await repository.create_task(
        name="Failed",
        user_id="owner",
        status="failed",
        error="Prior failure",
        params={"executor_id": "aggregate_results_executor"},
        token_usage={"total": 7},
    )
    await RunService(use_test_db_session).ensure_run(task.id)
    before = task.to_dict()
    receipt = await retry_run(repository, task.id, "retry", "owner")
    result = await TaskExecutor().execute_task_by_id(
        receipt["run_id"], db_session=use_test_db_session
    )
    assert result["status"] == "completed"
    original = await repository.get_task_by_id(task.id)
    assert original is not None and original.to_dict() == before
    fresh = await repository.get_task_by_id(receipt["run_id"])
    assert fresh is not None and fresh.error is None and fresh.result is not None
    assert fresh.token_usage is None


@pytest.mark.asyncio
async def test_reexecute_failed_parent_reuses_completed_dependency(
    use_test_db_session: Session | AsyncSession,
) -> None:
    repository = TaskRepository(use_test_db_session)
    parent = await repository.create_task(
        name="Parent",
        user_id="owner",
        status="failed",
        error="Prior failure",
        params={"executor_id": "aggregate_results_executor"},
    )
    child = await repository.create_task(
        name="Child",
        user_id="owner",
        parent_id=parent.id,
        status="completed",
        result={"value": 7},
        token_usage={"total": 10},
    )
    parent.dependencies = [{"id": child.id, "required": True}]
    await repository.db.commit()
    await RunService(use_test_db_session).ensure_run(parent.id)
    before = child.to_dict()
    receipt = await retry_run(repository, parent.id, "retry-tree", "owner")
    result = await TaskExecutor().execute_task_by_id(
        receipt["run_id"], db_session=use_test_db_session
    )
    child_id = receipt["task_mapping"][child.id]
    assert result["status"] == "completed"
    assert result["result"]["results"] == {child_id: {"value": 7}}
    reused = await repository.get_task_by_id(child_id)
    assert reused is not None and reused.status == "completed" and reused.token_usage is None
    original = await repository.get_task_by_id(child.id)
    assert original is not None and original.to_dict() == before
    with pytest.raises(ModuleError) as repeated:
        await TaskExecutor().execute_task_by_id(receipt["run_id"], db_session=use_test_db_session)
    assert repeated.value.code == "RUN_NOT_EXECUTABLE"


@pytest.mark.asyncio
async def test_pending_tasks_execute_once(use_test_db_session: Session | AsyncSession) -> None:
    repository = TaskRepository(use_test_db_session)
    task = await repository.create_task(
        name="Pending",
        params={"executor_id": "aggregate_results_executor"},
    )
    result: dict[str, Any] = await TaskExecutor().execute_task_by_id(
        task.id, db_session=use_test_db_session
    )
    assert result["status"] == "completed"
    fresh = await repository.get_task_by_id(task.id)
    assert fresh is not None and fresh.status == "completed" and fresh.result is not None


@pytest.mark.asyncio
async def test_completed_run_rejects_reexecution_without_changing_results(
    use_test_db_session: Session | AsyncSession,
) -> None:
    repository = TaskRepository(use_test_db_session)
    task = await repository.create_task(
        name="Completed",
        params={"executor_id": "aggregate_results_executor"},
    )
    await TaskExecutor().execute_task_by_id(task.id, db_session=use_test_db_session)
    completed = await repository.get_task_by_id(task.id)
    assert completed is not None
    before = completed.to_dict()
    with pytest.raises(ModuleError) as failure:
        await TaskExecutor().execute_task_by_id(task.id, db_session=use_test_db_session)
    assert failure.value.code == "RUN_NOT_EXECUTABLE"
    after = await repository.get_task_by_id(task.id)
    assert after is not None and after.to_dict() == before
