"""Regression: the circuit breaker must not count client/validation errors.

A BusinessError (bad task input) is not an executor-health signal; recording it
would trip the breaker OPEN and reject otherwise-healthy tasks for that executor.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apflow.core.execution.errors import ValidationError
from apflow.core.execution.task_manager import TaskManager
from apflow.durability.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitState,
)


def test_breaker_ignores_business_error_but_records_system_error() -> None:
    # _record_breaker_failure only reads task.params + the registry, so a bare
    # in-memory session (no schema, no queries) is enough to build TaskManager.
    session = sessionmaker(bind=create_engine("sqlite:///:memory:"))()
    task_manager = TaskManager(db=session, executor_instances={})

    registry = CircuitBreakerRegistry()
    # threshold 1 → a single recorded failure flips CLOSED -> OPEN.
    registry.get("rest", CircuitBreakerConfig(failure_threshold=1))
    task_manager._circuit_breaker_registry = registry

    task = SimpleNamespace(id="t1", params={"executor_id": "rest"}, schemas={})

    # A validation error (bad client input) must NOT trip the breaker.
    task_manager._record_breaker_failure(task, ValidationError("url is required"))
    assert registry.get("rest").state == CircuitState.CLOSED

    # A genuine system/executor fault MUST.
    task_manager._record_breaker_failure(task, RuntimeError("downstream 500"))
    assert registry.get("rest").state == CircuitState.OPEN
