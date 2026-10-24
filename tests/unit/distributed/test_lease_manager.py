"""Tests for LeaseManager distributed service."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from apflow.core.distributed.lease_manager import LeaseManager
from apflow.core.storage.sqlalchemy.models import (
    TaskLease,
    TaskModel,
    WorkflowRun,
)


class TestLeaseManager:
    """Tests for LeaseManager service methods."""

    def test_acquire_lease_success(
        self, session_factory, config, session, sample_node, sample_task
    ):
        """Acquiring a lease on an unleased task succeeds."""
        manager = LeaseManager(session_factory, config)

        lease = manager.acquire_lease("task-1", "worker-1")

        assert lease is not None
        assert lease.task_id == "task-1"
        assert lease.node_id == "worker-1"
        assert len(lease.lease_token) == 32
        assert lease.expires_at > datetime.now(timezone.utc)

    def test_acquire_lease_already_leased(
        self, session_factory, config, session, sample_node, sample_task
    ):
        """Acquiring a lease on an already-leased task returns None."""
        manager = LeaseManager(session_factory, config)

        first = manager.acquire_lease("task-1", "worker-1")
        assert first is not None

        second = manager.acquire_lease("task-1", "worker-1")
        assert second is None

    def test_renew_lease_extends_expiry(
        self, session_factory, config, session, sample_node, sample_task
    ):
        """Renewing a valid lease extends its expiry time."""
        manager = LeaseManager(session_factory, config)

        lease = manager.acquire_lease("task-1", "worker-1")
        assert lease is not None

        old_expiry = lease.expires_at
        result = manager.renew_lease(lease.lease_token)

        assert result is True

        # Verify expiry was extended
        session.expire_all()
        updated = session.get(TaskLease, "task-1")
        assert updated is not None
        assert updated.expires_at > old_expiry

    def test_renew_lease_invalid_token_fails(self, session_factory, config):
        """Renewing with an invalid token returns False."""
        manager = LeaseManager(session_factory, config)

        result = manager.renew_lease("nonexistent-token-abcdef1234567890")
        assert result is False

    def test_renew_lease_expired_fails(
        self, session_factory, config, session, sample_node, sample_task
    ):
        """Renewing an expired lease returns False."""
        manager = LeaseManager(session_factory, config)

        lease = manager.acquire_lease("task-1", "worker-1")
        assert lease is not None

        # Force the lease to be expired
        db_lease = session.get(TaskLease, "task-1")
        db_lease.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        session.commit()

        result = manager.renew_lease(lease.lease_token)
        assert result is False

    def test_cleanup_expired_leases(
        self, session_factory, config, session, sample_node, sample_task
    ):
        """Cleanup preserves interrupted execution history and requires reconciliation."""
        manager = LeaseManager(session_factory, config)

        lease = manager.acquire_lease("task-1", "worker-1")
        assert lease is not None

        # Mark task as in_progress and expire the lease
        task = session.get(TaskModel, "task-1")
        task.status = "in_progress"
        db_lease = session.get(TaskLease, "task-1")
        db_lease.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        session.commit()

        cleaned = manager.cleanup_expired_leases()

        assert "task-1" in cleaned

        # Verify lease removed
        session.expire_all()
        remaining = session.get(TaskLease, "task-1")
        assert remaining is None

        # Losing a lease cannot prove the remote operation did not happen.
        task = session.get(TaskModel, "task-1")
        assert task.status == "in_progress"
        assert task.error_info["delivery_state"] == "unknown"
        run = session.get(WorkflowRun, "task-1")
        assert run is not None and run.status == "reconciliation_required"
        assert manager.acquire_lease("task-1", "worker-1") is None

    def test_cleanup_expired_leases_does_not_report_already_completed_task(
        self, session_factory, config, session, sample_node, sample_task
    ):
        """Regression: cleanup_expired_leases unconditionally appended
        task_id (and thus fired a task_reassigned event) even when a racing
        worker had already completed the task normally before the expired
        lease was swept up. The lease row must still be cleaned up, but the
        task must NOT be reported as reassigned. (Review CRITICAL #21)
        """
        manager = LeaseManager(session_factory, config)

        lease = manager.acquire_lease("task-1", "worker-1")
        assert lease is not None

        # A racing worker completed the task normally, then the lease
        # expired before its row was cleaned up.
        task = session.get(TaskModel, "task-1")
        task.status = "completed"
        db_lease = session.get(TaskLease, "task-1")
        db_lease.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        session.commit()

        with patch("apflow.core.distributed.lease_manager.emit_task_event") as mock_emit:
            cleaned = manager.cleanup_expired_leases()

        # The stale lease row is still removed...
        session.expire_all()
        assert session.get(TaskLease, "task-1") is None
        # ...but the already-completed task is not reported as reassigned.
        assert "task-1" not in cleaned
        mock_emit.assert_not_called()
        task = session.get(TaskModel, "task-1")
        assert task.status == "completed"

    def test_release_lease_removes_entry(
        self, session_factory, config, session, sample_node, sample_task
    ):
        """Releasing a lease with the correct token removes it."""
        manager = LeaseManager(session_factory, config)

        lease = manager.acquire_lease("task-1", "worker-1")
        assert lease is not None

        manager.release_lease("task-1", lease.lease_token)

        session.expire_all()
        remaining = session.get(TaskLease, "task-1")
        assert remaining is None

    def test_concurrent_lease_acquisition_only_one_wins(
        self, session_factory, config, session, sample_node, sample_task
    ):
        """When multiple nodes attempt to acquire the same lease, only one wins."""
        manager = LeaseManager(session_factory, config)
        results = []
        for i in range(5):
            result = manager.acquire_lease("task-1", "worker-1")
            results.append(result)

        winners = [r for r in results if r is not None]
        assert len(winners) == 1

    def test_release_lease_wrong_token_no_effect(
        self, session_factory, config, session, sample_node, sample_task
    ):
        """Release with wrong token does not delete the lease."""
        manager = LeaseManager(session_factory, config)
        lease = manager.acquire_lease("task-1", "worker-1")
        assert lease is not None

        manager.release_lease("task-1", "wrong-token")

        # Lease should still exist - node can still renew
        success = manager.renew_lease(str(lease.lease_token))
        assert success is True


class TestLeaseManagerORMFallback:
    """Tests for the ORM-based fallback lease acquisition path."""

    def test_acquire_lease_orm_path(
        self, session_factory, config, session, sample_node, sample_task
    ):
        """ORM path acquires lease when is_postgresql returns False."""
        manager = LeaseManager(session_factory, config)
        with patch(
            "apflow.core.distributed.lease_manager.is_postgresql",
            return_value=False,
        ):
            lease = manager.acquire_lease("task-1", "worker-1")
        assert lease is not None
        assert lease.task_id == "task-1"
        assert lease.node_id == "worker-1"
        assert len(lease.lease_token) == 32

    def test_acquire_lease_orm_path_existing_active_lease(
        self, session_factory, config, session, sample_node, sample_task
    ):
        """ORM path returns None when active lease exists."""
        manager = LeaseManager(session_factory, config)
        with patch(
            "apflow.core.distributed.lease_manager.is_postgresql",
            return_value=False,
        ):
            lease1 = manager.acquire_lease("task-1", "worker-1")
            lease2 = manager.acquire_lease("task-1", "worker-1")
        assert lease1 is not None
        assert lease2 is None

    def test_acquire_lease_orm_path_expired_lease_replaced(
        self, session_factory, config, session, sample_node, sample_task
    ):
        """ORM path replaces expired lease."""
        manager = LeaseManager(session_factory, config)
        with patch(
            "apflow.core.distributed.lease_manager.is_postgresql",
            return_value=False,
        ):
            lease1 = manager.acquire_lease("task-1", "worker-1")
            assert lease1 is not None

            # Time-travel past expiry so the lease is expired
            future = datetime.now(timezone.utc) + timedelta(
                seconds=config.lease_duration_seconds + 10
            )
            with patch(
                "apflow.core.distributed.lease_manager._utcnow",
                return_value=future,
            ):
                lease2 = manager.acquire_lease("task-1", "worker-1")

        assert lease2 is not None
        assert lease2.node_id == "worker-1"


class TestLeaseManagerValidation:
    """Tests for input validation guards in LeaseManager."""

    def test_acquire_lease_empty_task_id_raises(self, session_factory, config):
        """acquire_lease rejects empty task_id."""
        manager = LeaseManager(session_factory, config)
        with pytest.raises(ValueError, match="must not be empty"):
            manager.acquire_lease("", "node-1")

    def test_acquire_lease_empty_node_id_raises(self, session_factory, config):
        """acquire_lease rejects empty node_id."""
        manager = LeaseManager(session_factory, config)
        with pytest.raises(ValueError, match="must not be empty"):
            manager.acquire_lease("task-1", "")

    def test_renew_lease_empty_token_raises(self, session_factory, config):
        """renew_lease rejects empty lease_token."""
        manager = LeaseManager(session_factory, config)
        with pytest.raises(ValueError, match="must not be empty"):
            manager.renew_lease("")

    def test_release_lease_empty_task_id_raises(self, session_factory, config):
        """release_lease rejects empty task_id."""
        manager = LeaseManager(session_factory, config)
        with pytest.raises(ValueError, match="must not be empty"):
            manager.release_lease("", "token")

    def test_release_lease_empty_token_raises(self, session_factory, config):
        """release_lease rejects empty lease_token."""
        manager = LeaseManager(session_factory, config)
        with pytest.raises(ValueError, match="must not be empty"):
            manager.release_lease("task-1", "")


class TestCleanupPreservesExecutionHistory:
    """Lease expiry preserves the complete legacy structural root and execution generation."""

    def test_child_lease_enrolls_whole_tree_without_resetting_generation(
        self, session_factory, config, session, sample_node, sample_task
    ):
        child = TaskModel.create(
            {
                "id": "child",
                "name": "Child",
                "parent_id": "task-1",
                "status": "in_progress",
                "attempt_id": 4,
            }
        )
        session.add(child)
        session.commit()
        manager = LeaseManager(session_factory, config)
        lease = manager.acquire_lease("child", "worker-1")
        assert lease is not None
        session.get(TaskLease, "child").expires_at = datetime.now(timezone.utc) - timedelta(
            seconds=5
        )
        session.commit()
        manager.cleanup_expired_leases()
        session.expire_all()
        child = session.get(TaskModel, "child")
        root = session.get(TaskModel, "task-1")
        run = session.get(WorkflowRun, "task-1")
        assert child.status == "in_progress" and child.attempt_id == 4
        assert root.status == "pending"
        assert child.run_id == root.run_id == run.id
        assert run.status == "reconciliation_required"
        assert {row["id"] for row in run.definition_snapshot["tasks"]} == {"task-1", "child"}

    def test_expired_invocation_moves_reserved_budget_to_unknown_once(
        self, session_factory, config, session, sample_node, sample_task
    ):
        from apflow.core.storage.sqlalchemy.models import (
            BudgetAccount,
            BudgetReservation,
            ExecutionAttempt,
        )

        manager = LeaseManager(session_factory, config)
        lease = manager.acquire_lease("task-1", "worker-1")
        assert lease is not None
        task = session.get(TaskModel, "task-1")
        task.status = "in_progress"
        session.add_all(
            [
                BudgetAccount(scope_id="scope", limit_tokens=10, reserved_tokens=5),
                BudgetReservation(
                    id="invocation",
                    scope_id="scope",
                    task_id="task-1",
                    state="reserved",
                    reserved_tokens=5,
                ),
                ExecutionAttempt(
                    id="invocation",
                    task_id="task-1",
                    attempt_id=0,
                    attempt_number=1,
                    status="running",
                    started_at=datetime.now(timezone.utc),
                ),
            ]
        )
        session.get(TaskLease, "task-1").expires_at = datetime.now(timezone.utc) - timedelta(
            seconds=5
        )
        session.commit()
        manager.cleanup_expired_leases()
        manager.cleanup_expired_leases()
        session.expire_all()
        account = session.get(BudgetAccount, "scope")
        reservation = session.get(BudgetReservation, "invocation")
        invocation = session.get(ExecutionAttempt, "invocation")
        assert account.reserved_tokens == 0 and account.unknown_tokens == 5
        assert reservation.state == "unknown" and reservation.actual_tokens is None
        assert invocation.status == "unknown" and invocation.run_id == "task-1"
