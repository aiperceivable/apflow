"""Pre-call and dependency failures preserve their phase and delivery certainty."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from apflow.core.execution.errors import ValidationError
from apflow.core.execution.task_manager import TaskManager
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.core.types import TaskTreeNode
from apflow.durability.circuit_breaker import CircuitBreakerConfig, CircuitBreakerRegistry
from apflow.governance.budget import BudgetManager


@pytest.mark.asyncio
async def test_exhausted_budget_has_governance_phase_before_executor_call(
    sync_db_session: Session,
) -> None:
    repository = TaskRepository(sync_db_session)
    manager = TaskManager(
        sync_db_session, pre_hooks=[], post_hooks=[], budget_manager=BudgetManager(repository)
    )
    task = await repository.create_task(name="Exhausted", token_budget=0)
    await manager._execute_single_task(task)
    current = await repository.get_task_by_id(str(task.id))
    assert current is not None and current.status == "failed" and current.error_info is not None
    assert current.error_info["code"] == "BUDGET_EXHAUSTED"
    assert current.error_info["phase"] == "governance"
    assert current.error_info["delivery_state"] == "not_sent"


@pytest.mark.asyncio
async def test_open_breaker_has_admission_phase_and_no_invocation(sync_db_session: Session) -> None:
    registry = CircuitBreakerRegistry(default_config=CircuitBreakerConfig(failure_threshold=1))
    registry.get("blocked_executor").record_failure()
    manager = TaskManager(
        sync_db_session, pre_hooks=[], post_hooks=[], circuit_breaker_registry=registry
    )
    task = await manager.task_repository.create_task(
        name="Blocked", params={"executor_id": "blocked_executor"}
    )
    await manager._execute_single_task(task)
    current = await manager.task_repository.get_task_by_id(str(task.id))
    assert current is not None and current.error_info is not None
    assert current.error_info["code"] == "CIRCUIT_OPEN"
    assert current.error_info["phase"] == "admission"
    assert current.error_info["delivery_state"] == "not_sent"


@pytest.mark.asyncio
async def test_tree_dependency_check_failure_has_safe_structured_metadata(
    sync_db_session: Session,
) -> None:
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    task = await manager.task_repository.create_task(name="Dependency check")
    with patch(
        "apflow.core.execution.task_manager.are_dependencies_satisfied", new_callable=AsyncMock
    ) as check:
        check.side_effect = ValidationError("SYNTHETIC_SECRET")
        with pytest.raises(ValidationError):
            await manager._execute_task_tree_recursive(TaskTreeNode(task))
    current = await manager.task_repository.get_task_by_id(str(task.id))
    assert current is not None and current.error_info is not None
    assert current.error_info["phase"] == "dependency"
    assert current.error_info["category"] == "dependency"
    assert current.error_info["delivery_state"] == "not_sent"
    assert "SYNTHETIC_SECRET" not in str(current.error_info)


@pytest.mark.asyncio
async def test_dependent_trigger_fallback_retains_failure_origin(sync_db_session: Session) -> None:
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    source = await manager.task_repository.create_task(name="Source", status="completed")
    consumer = await manager.task_repository.create_task(name="Consumer", parent_id=source.id)
    with patch(
        "apflow.core.execution.task_manager.are_dependencies_satisfied",
        new_callable=AsyncMock,
        return_value=True,
    ):
        with patch.object(
            manager,
            "_execute_single_task",
            new_callable=AsyncMock,
            side_effect=RuntimeError("SYNTHETIC_SECRET"),
        ):
            await manager.execute_after_task(source)
    current = await manager.task_repository.get_task_by_id(str(consumer.id))
    assert current is not None and current.error_info is not None
    assert current.error_info["phase"] == "dependency_trigger"
    assert current.error_info["delivery_state"] == "unknown"
    assert "SYNTHETIC_SECRET" not in str(current.error_info)


@pytest.mark.asyncio
async def test_missing_executor_is_configuration_failure_not_unknown_delivery(
    sync_db_session: Session,
) -> None:
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    task = await manager.task_repository.create_task(
        name="Missing", params={"executor_id": "nonexistent_phase_probe"}
    )
    await manager._execute_single_task(task)
    current = await manager.task_repository.get_task_by_id(str(task.id))
    assert current is not None and current.error_info is not None
    assert current.error_info["phase"] == "configuration"
    assert current.error_info["delivery_state"] == "not_sent"
