"""
End-to-end tests for the scheduler's clone-per-fire run history.

These drive the real execution path (InternalScheduler._execute_task_via_db and
the webhook push path) against a real executor and the test database, verifying
that each fire produces an executed run instance while the definition itself
never executes in place.
"""

from typing import Any, Dict, Optional

import pytest

from apflow.core.config import get_task_model_class
from apflow.core.extensions.decorators import executor_register
from apflow.core.storage.sqlalchemy.models import TaskOriginType
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.extensions.core.aggregate_results_executor import AggregateResultsExecutor
from apflow.scheduler.internal import InternalScheduler


@executor_register(override=True)
class _TokenEmittingExecutor(AggregateResultsExecutor):
    """Test executor that returns a token_usage payload in its result."""

    id = "test_token_executor"
    name = "Test Token Executor"

    async def execute(self, inputs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"value": 42, "token_usage": {"input": 11, "output": 22, "total": 33}}


@executor_register(override=True)
class _FailingExecutor(AggregateResultsExecutor):
    """Test executor that always raises, to exercise the failed-run path."""

    id = "test_failing_executor"
    name = "Test Failing Executor"

    async def execute(self, inputs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raise RuntimeError("intentional test failure")


async def _make_scheduled_definition(repo: TaskRepository):
    """A schedulable definition backed by a real, always-completing executor."""
    return await repo.create_task(
        name="report",
        user_id="u1",
        status="pending",
        inputs={"resource": "cpu"},
        schemas={"method": "aggregate_results_executor"},
        schedule_type="interval",
        schedule_expression="3600",
        schedule_enabled=True,
        run_count=0,
    )


class TestSchedulerHistoryEndToEnd:
    """Real execution through the poll path."""

    @pytest.mark.asyncio
    async def test_fire_executes_run_instance_and_keeps_definition_clean(self, use_test_db_session):
        repo = TaskRepository(use_test_db_session, task_model_class=get_task_model_class())
        definition = await _make_scheduled_definition(repo)
        definition_id = definition.id

        scheduler = InternalScheduler()

        # First fire.
        await scheduler._execute_task_via_db(definition_id)

        # The definition never executed in place: no result, still a schedulable
        # definition, advanced by exactly one fire.
        definition = await repo.get_task_by_id(definition_id)
        assert definition is not None
        assert definition.result is None
        assert definition.origin_type != TaskOriginType.scheduled_run
        assert definition.schedule_enabled is True
        assert definition.run_count == 1

        # Exactly one run instance, actually executed to completion with a result.
        runs = await repo.list_scheduled_runs(definition_id)
        assert len(runs) == 1
        run = runs[0]
        assert run.id != definition_id
        assert run.origin_type == TaskOriginType.scheduled_run
        assert run.status == "completed"
        assert run.result is not None
        assert run.schedule_enabled is False

    @pytest.mark.asyncio
    async def test_repeated_fires_accumulate_independent_runs(self, use_test_db_session):
        repo = TaskRepository(use_test_db_session, task_model_class=get_task_model_class())
        definition = await _make_scheduled_definition(repo)
        definition_id = definition.id

        scheduler = InternalScheduler()
        await scheduler._execute_task_via_db(definition_id)
        await scheduler._execute_task_via_db(definition_id)
        await scheduler._execute_task_via_db(definition_id)

        runs = await repo.list_scheduled_runs(definition_id)
        # Three fires → three distinct, independently-executed run instances.
        assert len(runs) == 3
        assert len({r.id for r in runs}) == 3
        assert all(r.status == "completed" for r in runs)

        definition = await repo.get_task_by_id(definition_id)
        assert definition is not None
        assert definition.run_count == 3
        assert definition.result is None


class TestSchedulerHistoryWebhookPath:
    """Real execution through the push/webhook (schedule.trigger) path."""

    @pytest.mark.asyncio
    async def test_webhook_trigger_creates_executed_run(self, use_test_db_session):
        from apflow.scheduler.gateway.webhook import WebhookGateway

        repo = TaskRepository(use_test_db_session, task_model_class=get_task_model_class())
        definition = await _make_scheduled_definition(repo)
        definition_id = definition.id

        gateway = WebhookGateway()
        result = await gateway.trigger_task(definition_id, execute_async=False)

        # The trigger reports the run instance's outcome, not the definition's.
        assert result["success"] is True
        assert result["status"] == "completed"
        assert result["task_id"] == definition_id
        assert result["run_id"] != definition_id

        # History has the executed run; the definition stayed clean.
        runs = await repo.list_scheduled_runs(definition_id)
        assert len(runs) == 1
        assert runs[0].id == result["run_id"]
        assert runs[0].status == "completed"

        definition = await repo.get_task_by_id(definition_id)
        assert definition is not None
        assert definition.result is None
        assert definition.run_count == 1


class TestSchedulerHistoryComplexRuns:
    """Real execution of multi-step trees, failures, and token accounting."""

    @pytest.mark.asyncio
    async def test_multistep_workflow_clones_and_remaps_dependencies(self, use_test_db_session):
        """A scheduled parent+child+dependency tree fires as a fully cloned,
        dependency-remapped tree that executes end to end."""
        repo = TaskRepository(use_test_db_session, task_model_class=get_task_model_class())

        # child (leaf) — the parent depends on it and aggregates its result.
        child = await repo.create_task(
            name="fetch",
            user_id="u1",
            status="pending",
            inputs={"resource": "cpu"},
            schemas={"method": "aggregate_results_executor"},
        )
        # parent (root, scheduled) depends on the child.
        parent = await repo.create_task(
            name="aggregate",
            user_id="u1",
            status="pending",
            has_children=True,
            params={"executor_id": "aggregate_results_executor"},
            dependencies=[{"id": child.id, "required": True}],
            schedule_type="interval",
            schedule_expression="3600",
            schedule_enabled=True,
            run_count=0,
        )
        child.parent_id = parent.id
        use_test_db_session.commit()
        parent_id = parent.id
        child_id = child.id

        await InternalScheduler()._execute_task_via_db(parent_id)

        # The definition tree is untouched.
        parent = await repo.get_task_by_id(parent_id)
        assert parent is not None
        assert parent.result is None
        assert parent.run_count == 1

        # Exactly one run root, with a cloned child under it.
        runs = await repo.list_scheduled_runs(parent_id)
        assert len(runs) == 1
        run_root = runs[0]
        assert run_root.status == "completed"
        run_children = await repo.get_child_tasks_by_parent_id(run_root.id)
        assert len(run_children) == 1
        run_child = run_children[0]
        assert run_child.status == "completed"

        # The clone is a fresh tree: new ids, and the dependency was remapped to
        # the cloned child (not the original definition's child).
        assert run_root.id != parent_id
        assert run_child.id != child_id
        dep_ids = {d["id"] for d in (run_root.dependencies or [])}
        assert dep_ids == {run_child.id}
        assert child_id not in dep_ids

    @pytest.mark.asyncio
    async def test_failed_run_is_recorded_and_definition_advances(self, use_test_db_session):
        """A failing fire records a non-completed run in history and still advances
        the definition's schedule."""
        # The autouse fixture rebuilds the registry with built-ins only, so
        # re-register this test executor into the current registry before firing.
        executor_register(override=True)(_FailingExecutor)
        repo = TaskRepository(use_test_db_session, task_model_class=get_task_model_class())
        definition = await repo.create_task(
            name="flaky",
            user_id="u1",
            status="pending",
            schemas={"method": "test_failing_executor"},
            schedule_type="interval",
            schedule_expression="3600",
            schedule_enabled=True,
            run_count=0,
        )
        definition_id = definition.id

        await InternalScheduler()._execute_task_via_db(definition_id)

        runs = await repo.list_scheduled_runs(definition_id)
        assert len(runs) == 1
        assert runs[0].status != "completed"

        definition = await repo.get_task_by_id(definition_id)
        assert definition is not None
        assert definition.run_count == 1
        assert definition.result is None

    @pytest.mark.asyncio
    async def test_run_history_captures_token_usage(self, use_test_db_session):
        """The run instance's result carries the fire's token_usage payload."""
        # The autouse fixture rebuilds the registry with built-ins only, so
        # re-register this test executor into the current registry before firing.
        executor_register(override=True)(_TokenEmittingExecutor)
        repo = TaskRepository(use_test_db_session, task_model_class=get_task_model_class())
        definition = await repo.create_task(
            name="llm-job",
            user_id="u1",
            status="pending",
            schemas={"method": "test_token_executor"},
            schedule_type="interval",
            schedule_expression="3600",
            schedule_enabled=True,
            run_count=0,
        )
        definition_id = definition.id

        await InternalScheduler()._execute_task_via_db(definition_id)

        runs = await repo.list_scheduled_runs(definition_id)
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "completed"
        assert isinstance(run.result, dict)
        assert run.result.get("token_usage") == {"input": 11, "output": 22, "total": 33}


class TestDistributedDispatchOnly:
    """Leader-side dispatch: instantiate pending runs for workers, do not execute."""

    @pytest.mark.asyncio
    async def test_dispatch_only_creates_pending_run_without_executing(self, use_test_db_session):
        repo = TaskRepository(use_test_db_session, task_model_class=get_task_model_class())
        definition = await _make_scheduled_definition(repo)
        definition_id = definition.id

        # Dispatch-only scheduler (the leader's mode in a cluster).
        scheduler = InternalScheduler(dispatch_only=True)
        await scheduler._execute_task_via_db(definition_id)

        # A run instance exists but was NOT executed — it stays pending for a
        # distributed worker to lease. This is the whole point of the bridge.
        runs = await repo.list_scheduled_runs(definition_id)
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "pending"
        assert run.result is None
        assert run.origin_type == TaskOriginType.scheduled_run
        assert run.schedule_enabled is False

        # The definition's schedule still advanced (the dispatch succeeded).
        definition = await repo.get_task_by_id(definition_id)
        assert definition is not None
        assert definition.run_count == 1
        assert definition.result is None

    @pytest.mark.asyncio
    async def test_dispatch_only_repeated_fires_queue_independent_runs(self, use_test_db_session):
        repo = TaskRepository(use_test_db_session, task_model_class=get_task_model_class())
        definition = await _make_scheduled_definition(repo)
        definition_id = definition.id

        scheduler = InternalScheduler(dispatch_only=True)
        await scheduler._execute_task_via_db(definition_id)
        await scheduler._execute_task_via_db(definition_id)

        runs = await repo.list_scheduled_runs(definition_id)
        assert len(runs) == 2
        assert all(r.status == "pending" for r in runs)
        assert len({r.id for r in runs}) == 2


class TestScheduledDispatchKey:
    """Deterministic occurrence idempotency key (dimension B)."""

    def test_key_is_deterministic_and_slot_sensitive(self) -> None:
        from datetime import datetime, timezone

        from apflow.scheduler.internal import scheduled_dispatch_key

        class _Defn:
            def __init__(self, id_, nr):
                self.id = id_
                self.next_run_at = nr

        slot_a = datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc)
        slot_b = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)

        # Same definition + same slot → same key (idempotent across dispatches).
        assert scheduled_dispatch_key(_Defn("d1", slot_a)) == scheduled_dispatch_key(
            _Defn("d1", slot_a)
        )
        # Different slot → different key (distinct occurrences).
        assert scheduled_dispatch_key(_Defn("d1", slot_a)) != scheduled_dispatch_key(
            _Defn("d1", slot_b)
        )
        # Different definition → different key.
        assert scheduled_dispatch_key(_Defn("d1", slot_a)) != scheduled_dispatch_key(
            _Defn("d2", slot_a)
        )
        # No slot → no key (ad-hoc fires are each distinct).
        assert scheduled_dispatch_key(_Defn("d1", None)) is None

    @pytest.mark.asyncio
    async def test_fire_stamps_occurrence_key_on_run(self, use_test_db_session):
        from datetime import datetime, timezone

        from apflow.scheduler.internal import scheduled_dispatch_key

        repo = TaskRepository(use_test_db_session, task_model_class=get_task_model_class())
        definition = await repo.create_task(
            name="charge",
            user_id="u1",
            status="pending",
            inputs={"resource": "cpu"},
            schemas={"method": "aggregate_results_executor"},
            schedule_type="cron",
            schedule_expression="0 9 * * *",
            schedule_enabled=True,
            next_run_at=datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc),
        )
        expected_key = scheduled_dispatch_key(definition)

        await InternalScheduler(dispatch_only=True)._execute_task_via_db(definition.id)

        runs = await repo.list_scheduled_runs(definition.id)
        assert len(runs) == 1
        assert runs[0].idempotency_key == expected_key


class TestDuplicateDispatchAccounting:
    """F1: a rejected duplicate dispatch advances next_run_at but does not
    double-count run_count."""

    @pytest.mark.asyncio
    async def test_complete_scheduled_run_count_run_false(self, use_test_db_session):
        from datetime import datetime, timezone

        repo = TaskRepository(use_test_db_session, task_model_class=get_task_model_class())
        definition = await repo.create_task(
            name="sched",
            user_id="u1",
            status="pending",
            schedule_type="interval",
            schedule_expression="3600",
            schedule_enabled=True,
            next_run_at=datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc),
            run_count=5,
        )

        # count_run=False advances the schedule without bumping run_count.
        updated = await repo.complete_scheduled_run(definition.id, count_run=False)
        assert updated is not None
        assert updated.run_count == 5
        assert updated.next_run_at is not None
        assert updated.next_run_at != datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc)

        # Default (count_run=True) still increments.
        updated2 = await repo.complete_scheduled_run(definition.id)
        assert updated2 is not None
        assert updated2.run_count == 6

    @pytest.mark.asyncio
    async def test_rejected_duplicate_advances_but_does_not_count(self, use_test_db_session):
        from datetime import datetime, timezone

        from apflow.core.execution.task_creator import TaskCreator
        from apflow.scheduler.internal import scheduled_dispatch_key

        repo = TaskRepository(use_test_db_session, task_model_class=get_task_model_class())
        definition = await repo.create_task(
            name="charge",
            user_id="u1",
            status="pending",
            inputs={"resource": "cpu"},
            schemas={"method": "aggregate_results_executor"},
            schedule_type="interval",
            schedule_expression="3600",
            schedule_enabled=True,
            next_run_at=datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc),
            run_count=0,
        )
        key = scheduled_dispatch_key(definition)

        # A peer node already dispatched this occurrence (occupies the key).
        prior = await TaskCreator(use_test_db_session).instantiate_scheduled_run(
            definition, idempotency_key=key
        )
        assert prior is not None
        before = await repo.get_task_by_id(definition.id)
        assert before is not None
        prev_next = before.next_run_at

        # This node fires the same slot → instantiate collides → skip.
        await InternalScheduler(dispatch_only=True)._execute_task_via_db(definition.id)

        after = await repo.get_task_by_id(definition.id)
        assert after is not None
        # No duplicate run created.
        assert len(await repo.list_scheduled_runs(definition.id)) == 1
        # run_count NOT double-counted by the rejected dispatch.
        assert after.run_count == 0
        # ...but next_run_at DID advance (progress guaranteed even on rejection).
        assert after.next_run_at != prev_next
