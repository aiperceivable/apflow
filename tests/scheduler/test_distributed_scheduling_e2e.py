"""
Real distributed-scheduling end-to-end test (requires PostgreSQL).

Exercises the full cluster path: the leader's dispatch-only scheduler turns a due
schedule into a pending run instance, and a real WorkerRuntime leases and executes
that run instance — while never touching the recurring definition itself.

Skipped automatically when the test database is not PostgreSQL (the distributed
coordination primitives require Postgres' atomic guarantees).
"""

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apflow.core.config import get_task_model_class
from apflow.core.execution.task_executor import TaskExecutor
from apflow.core.storage.sqlalchemy.models import TaskOriginType
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.scheduler.internal import InternalScheduler


def _test_db_url() -> str | None:
    from tests.conftest import _get_test_database_url

    return _get_test_database_url()


def _is_postgres() -> bool:
    url = _test_db_url()
    return bool(url and url.startswith("postgres"))


pytestmark = pytest.mark.skipif(
    not _is_postgres(), reason="distributed scheduling E2E requires a PostgreSQL test database"
)


def _make_worker(node_id: str, task_executor=None):
    """Build a real WorkerRuntime bound to the Postgres test database.

    Real distributed managers (node registry, lease manager, idempotency) run
    against Postgres. task_executor defaults to the real single-task executor;
    pass a stub to observe whether execution was actually reached.
    """
    from apflow.core.distributed.config import DistributedConfig
    from apflow.core.distributed.idempotency import IdempotencyManager
    from apflow.core.distributed.lease_manager import LeaseManager
    from apflow.core.distributed.node_registry import NodeRegistry
    from apflow.core.distributed.worker import WorkerRuntime

    url = _test_db_url()
    assert url is not None  # guarded by pytestmark skipif
    engine = create_engine(url)
    session_factory = sessionmaker(bind=engine)

    config = DistributedConfig(
        enabled=True,
        node_id=node_id,
        max_parallel_tasks_per_node=4,
    )
    node_registry = NodeRegistry(session_factory, config)
    lease_manager = LeaseManager(session_factory, config)
    idempotency = IdempotencyManager(session_factory)
    node_registry.register_node(node_id=node_id, executor_types=["default"], capabilities={})

    async def default_executor(task: Any) -> dict:
        return await TaskExecutor().execute_tasks([{"id": task.id}], require_existing_tasks=True)

    worker = WorkerRuntime(
        node_id=node_id,
        config=config,
        session_factory=session_factory,
        node_registry=node_registry,
        lease_manager=lease_manager,
        idempotency_manager=idempotency,
        task_executor=task_executor or default_executor,
    )
    return worker, engine


class TestDistributedSchedulingEndToEnd:
    @pytest.mark.asyncio
    async def test_leader_dispatches_worker_executes(self, use_test_db_session):
        repo = TaskRepository(use_test_db_session, task_model_class=get_task_model_class())

        # A schedulable definition backed by a real executor.
        definition = await repo.create_task(
            name="cluster-report",
            user_id="u1",
            status="pending",
            inputs={"resource": "cpu"},
            schemas={"method": "aggregate_results_executor"},
            schedule_type="interval",
            schedule_expression="3600",
            schedule_enabled=True,
            run_count=0,
        )
        definition_id = definition.id

        # 1) The leader's dispatch-only scheduler turns the due schedule into a
        #    pending run instance, without executing it.
        await InternalScheduler(dispatch_only=True)._execute_task_via_db(definition_id)

        runs = await repo.list_scheduled_runs(definition_id)
        assert len(runs) == 1
        run_id = runs[0].id
        assert runs[0].status == "pending"
        assert runs[0].origin_type == TaskOriginType.scheduled_run

        worker, engine = _make_worker("e2e-worker-1")
        try:
            # 2) The worker sees the run instance as executable — but NOT the
            #    recurring definition (it is a template, excluded from execution).
            executable_ids = {t.id for t in worker._find_executable_tasks()}
            assert run_id in executable_ids
            assert definition_id not in executable_ids

            # 3) The worker leases and executes the run instance.
            run_task = next(t for t in worker._find_executable_tasks() if t.id == run_id)
            await worker._execute_task(run_task)
        finally:
            engine.dispose()

        # 4) The run instance executed to completion.
        run_after = await repo.get_task_by_id(run_id)
        assert run_after is not None
        assert run_after.status == "completed"

        # 5) The definition was never executed: still pending, no result, and its
        #    schedule advanced by the single dispatch.
        definition = await repo.get_task_by_id(definition_id)
        assert definition is not None
        assert definition.status == "pending"
        assert definition.result is None
        assert definition.run_count == 1

    @pytest.mark.asyncio
    async def test_lease_prevents_double_execution(self, use_test_db_session):
        """A run already leased by one node is not executed by another node —
        lease exclusivity prevents double execution across the cluster."""
        repo = TaskRepository(use_test_db_session, task_model_class=get_task_model_class())
        definition = await repo.create_task(
            name="cluster-once",
            user_id="u1",
            status="pending",
            inputs={"resource": "cpu"},
            schemas={"method": "aggregate_results_executor"},
            schedule_type="interval",
            schedule_expression="3600",
            schedule_enabled=True,
        )
        await InternalScheduler(dispatch_only=True)._execute_task_via_db(definition.id)
        runs = await repo.list_scheduled_runs(definition.id)
        run_id = runs[0].id

        # Node A holds the lease on the run (as if it is executing it).
        worker_a, engine_a = _make_worker("e2e-node-a")
        # Node B has a recording executor so we can prove it never runs the task.
        b_executed: list[str] = []

        async def recording_executor(task: Any) -> dict:
            b_executed.append(task.id)
            return {"status": "completed"}

        worker_b, engine_b = _make_worker("e2e-node-b", task_executor=recording_executor)
        try:
            lease_a = worker_a._lease_manager.acquire_lease(run_id, "e2e-node-a")
            assert lease_a is not None

            # Node B sees the run as executable but cannot lease it → skips execution.
            run_task = next(t for t in worker_b._find_executable_tasks() if t.id == run_id)
            await worker_b._execute_task(run_task)

            assert b_executed == []  # B never executed the already-leased run
        finally:
            engine_a.dispose()
            engine_b.dispose()
