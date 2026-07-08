"""Regression tests for distributed runtime/worker review fixes.

All mock-based (no live PostgreSQL): they exercise the fixed control-flow /
placement / shutdown logic directly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from apflow.core.distributed.config import DistributedConfig
from apflow.core.distributed.runtime import DistributedRuntime
from apflow.core.distributed.worker import WorkerRuntime


def _worker(config: DistributedConfig, lease_return=MagicMock()) -> WorkerRuntime:
    lease_manager = MagicMock()
    lease_manager.acquire_lease = MagicMock(return_value=lease_return)
    lease_manager.release_lease = MagicMock()
    return WorkerRuntime(
        "n1", config, MagicMock(), MagicMock(), lease_manager, MagicMock(), AsyncMock()
    )


def test_from_env_parses_node_executor_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APFLOW_NODE_EXECUTOR_TYPES", "rest, email")
    cfg = DistributedConfig.from_env()
    assert cfg.node_executor_types == ["rest", "email"]


def test_node_eligibility_uses_configured_executor_types() -> None:
    """Regression: the self-check used a hardcoded ['default'] profile, so any
    require_executors beyond 'default' starved. A node configured for 'rest' must
    now be eligible for a rest-constrained task."""
    worker = _worker(DistributedConfig(node_executor_types=["rest"]))
    ok = SimpleNamespace(id="T", placement_constraints={"require_executors": ["rest"]})
    bad = SimpleNamespace(id="T2", placement_constraints={"require_executors": ["gpu"]})
    assert worker._is_eligible_for_this_node(ok) is True
    assert worker._is_eligible_for_this_node(bad) is False


@pytest.mark.asyncio
async def test_execute_task_pops_running_tasks_on_lost_lease() -> None:
    """Regression (CRITICAL): losing the acquire race returned early before the
    finally that pops _running_tasks, leaking the entry and permanently blocking
    re-attempts by this worker."""
    worker = _worker(DistributedConfig(), lease_return=None)
    task = SimpleNamespace(id="T", attempt_id=0, inputs={})
    worker._running_tasks["T"] = MagicMock()  # as _poll_loop registers it

    await worker._execute_task(task)

    assert "T" not in worker._running_tasks


@pytest.mark.asyncio
async def test_runtime_shutdown_does_not_double_deregister_with_worker() -> None:
    """Regression (CRITICAL): a worker node deregistered twice (worker + runtime),
    the second raising NodeNotFoundError. The runtime must skip its own deregister
    when the worker runtime already did it."""
    runtime = DistributedRuntime(DistributedConfig(node_id="n1"), MagicMock())
    runtime._node_registry = MagicMock()
    runtime._worker_runtime = AsyncMock()
    runtime._role = "worker"

    await runtime.shutdown()

    runtime._worker_runtime.shutdown.assert_awaited_once()
    runtime._node_registry.deregister_node.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_shutdown_deregisters_when_no_worker_runtime() -> None:
    runtime = DistributedRuntime(DistributedConfig(node_id="n1"), MagicMock())
    runtime._node_registry = MagicMock()
    runtime._worker_runtime = None
    runtime._role = "observer"

    await runtime.shutdown()

    runtime._node_registry.deregister_node.assert_called_once_with("n1")
