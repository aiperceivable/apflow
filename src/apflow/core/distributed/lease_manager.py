"""Lease manager service for distributed task execution.

Manages task lease acquisition, renewal, release, and cleanup
to ensure exactly-once execution semantics across worker nodes.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from apflow.core.distributed.config import (
    DistributedConfig,
    as_utc,
    is_postgresql,
    utcnow as _utcnow,
)
from apflow.core.distributed.events import emit_task_event
from apflow.core.storage.sqlalchemy.models import (
    BudgetAccount,
    BudgetReservation,
    ExecutionAttempt,
    TaskDispatch,
    TaskLease,
    TaskModel,
    WorkflowRun,
)
from apflow.logger import get_logger

logger = get_logger(__name__)


class LeaseManager:
    """Manages task leases: acquire, renew, release, and cleanup."""

    def __init__(self, session_factory: sessionmaker, config: DistributedConfig) -> None:
        self._session_factory = session_factory
        self._config = config

    def acquire_lease(self, task_id: str, node_id: str) -> TaskLease | None:
        """Acquire a lease on a task for a given node.

        Returns the created TaskLease if successful, or None if the task
        already has an active (non-expired) lease or lost a race.
        """
        if not task_id or not node_id:
            raise ValueError("task_id and node_id must not be empty")

        with self._session_factory() as session:
            # Share the admission lock with durable submission before either owner is recorded.
            locked = session.execute(
                update(TaskModel)
                .where(TaskModel.id == task_id)
                .values(id=TaskModel.id, updated_at=TaskModel.updated_at)
            )
            if not locked.rowcount:
                session.rollback()
                return None
            if session.scalar(select(TaskDispatch.task_id).where(TaskDispatch.task_id == task_id)):
                session.rollback()
                return None
            run = session.get(WorkflowRun, task_id)
            if run is not None and run.status != "accepted":
                session.rollback()
                return None
            if not self._recover_expired_owner(session, task_id):
                return None
            if is_postgresql(session):
                return self._acquire_lease_postgresql(session, task_id, node_id)
            return self._acquire_lease_default(session, task_id, node_id)

    def _recover_expired_owner(self, session: Session, task_id: str) -> bool:
        """Lease expiry never authorizes replay of a previously started legacy tree."""
        lease = session.get(TaskLease, task_id)
        now = _utcnow()
        if lease is None or as_utc(lease.expires_at) > now:
            return True
        removed = session.execute(
            delete(TaskLease)
            .where(
                TaskLease.task_id == task_id,
                TaskLease.lease_token == lease.lease_token,
                TaskLease.expires_at <= now,
            )
            .returning(TaskLease.task_id)
            .execution_options(synchronize_session="fetch")
        ).scalar_one_or_none()
        if removed is None:
            session.rollback()
            return False
        task = session.get(TaskModel, task_id)
        assert task is not None
        run, tasks = self._recover_run(session, task)
        self._reconcile_run(session, run, tasks, now)
        if run.status != "accepted":
            session.commit()
            return False
        return True

    def _acquire_lease_postgresql(
        self, session: Session, task_id: str, node_id: str
    ) -> TaskLease | None:
        """Atomic lease acquisition using PostgreSQL INSERT ON CONFLICT."""
        now = _utcnow()
        lease_token = uuid.uuid4().hex[:32]
        expires_at = now + timedelta(seconds=self._config.lease_duration_seconds)

        # Delete expired lease first
        session.execute(
            text("DELETE FROM apflow_task_leases " "WHERE task_id = :tid AND expires_at <= :now"),
            {"tid": task_id, "now": now},
        )

        # Atomic insert; DO NOTHING if a live lease exists
        result = session.execute(
            text(
                "INSERT INTO apflow_task_leases "
                "(task_id, node_id, lease_token, expires_at) "
                "VALUES (:tid, :nid, :tok, :exp) "
                "ON CONFLICT (task_id) DO NOTHING "
                "RETURNING task_id, node_id, lease_token, expires_at"
            ),
            {
                "tid": task_id,
                "nid": node_id,
                "tok": lease_token,
                "exp": expires_at,
            },
        )
        row = result.fetchone()
        session.commit()

        if row is None:
            logger.info("Lease already held for task %s", task_id)
            return None

        # Re-fetch ORM object so callers get a proper TaskLease
        lease = session.get(TaskLease, task_id)
        logger.info(
            "Lease acquired for task %s by node %s (token=%s)",
            task_id,
            node_id,
            lease_token,
        )
        return lease

    def _acquire_lease_default(
        self, session: Session, task_id: str, node_id: str
    ) -> TaskLease | None:
        """ORM-based lease acquisition for SQLite and other dialects."""
        existing = session.get(TaskLease, task_id)
        now = _utcnow()

        # SQLAlchemy Column[DateTime] comparison not recognized by pyright as supporting > operator
        if existing is not None and as_utc(existing.expires_at) > now:  # type: ignore[operator]
            logger.info(
                "Lease already held for task %s by node %s",
                task_id,
                existing.node_id,
            )
            return None

        # Remove expired lease if present
        if existing is not None:
            session.delete(existing)
            session.flush()

        lease_token = uuid.uuid4().hex[:32]
        expires_at = now + timedelta(seconds=self._config.lease_duration_seconds)

        lease = TaskLease(
            task_id=task_id,
            node_id=node_id,
            lease_token=lease_token,
            expires_at=expires_at,
        )
        session.add(lease)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            logger.info("Lease acquisition race lost for task %s", task_id)
            return None
        logger.info(
            "Lease acquired for task %s by node %s (token=%s)",
            task_id,
            node_id,
            lease_token,
        )
        return lease

    def renew_lease(self, lease_token: str) -> bool:
        """Renew an existing lease by extending its expiry.

        Uses an atomic UPDATE with a WHERE clause that checks the token
        and expiry to prevent TOCTOU races where a stale worker could
        accidentally extend a different node's lease.

        Returns True if renewal succeeded, False if the token is invalid
        or the lease has expired.
        """
        if not lease_token:
            raise ValueError("lease_token must not be empty")

        with self._session_factory() as session:
            now = _utcnow()
            new_expires = now + timedelta(seconds=self._config.lease_duration_seconds)
            result = session.execute(
                text(
                    "UPDATE apflow_task_leases "
                    "SET expires_at = :exp "
                    "WHERE lease_token = :tok AND expires_at > :now"
                ),
                {"exp": new_expires, "tok": lease_token, "now": now},
            )
            session.commit()

            if result.rowcount > 0:
                logger.info("Lease renewed (token=%s)", lease_token)
                return True

            logger.info("Lease renewal failed: token %s not found or expired", lease_token)
            return False

    def release_lease(self, task_id: str, lease_token: str) -> None:
        """Release a lease on a task, verifying the token matches.

        Uses an atomic DELETE with a WHERE clause to prevent TOCTOU races
        where a stale worker could delete a different node's lease.
        """
        if not task_id or not lease_token:
            raise ValueError("task_id and lease_token must not be empty")

        with self._session_factory() as session:
            result = session.execute(
                text(
                    "DELETE FROM apflow_task_leases " "WHERE task_id = :tid AND lease_token = :tok"
                ),
                {"tid": task_id, "tok": lease_token},
            )
            session.commit()

            if result.rowcount > 0:
                logger.info("Lease released for task %s", task_id)
            else:
                logger.info(
                    "Lease release skipped for task %s: token mismatch or not found",
                    task_id,
                )

    def cleanup_expired_leases(self) -> list[str]:
        """Remove expired ownership; preserve every started execution for reconciliation."""
        cleaned: list[str] = []
        with self._session_factory() as session:
            now = _utcnow()
            leases = session.query(TaskLease).filter(TaskLease.expires_at <= now).all()
            for lease in leases:
                removed = session.execute(
                    delete(TaskLease)
                    .where(
                        TaskLease.task_id == lease.task_id,
                        TaskLease.lease_token == lease.lease_token,
                        TaskLease.expires_at <= now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if not removed.rowcount:
                    continue
                task = session.get(TaskModel, lease.task_id)
                if task is None:
                    continue
                run, tasks = self._recover_run(session, task)
                if self._reconcile_run(session, run, tasks, now):
                    cleaned.append(lease.task_id)
            session.commit()
        for task_id in cleaned:
            emit_task_event(self._session_factory, task_id, "task_reconciliation_required")
        return cleaned

    def recover_lease(self, task_id: str, lease_token: str) -> bool:
        """Relinquish a matching owner and preserve its recovery state atomically."""
        if not task_id or not lease_token:
            raise ValueError("task_id and lease_token must not be empty")
        reconcile = False
        with self._session_factory() as session:
            # Use the same root lock as admission and lease acquisition.
            session.execute(
                update(TaskModel)
                .where(TaskModel.id == task_id)
                .values(updated_at=TaskModel.updated_at)
            )
            removed = session.execute(
                delete(TaskLease)
                .where(
                    TaskLease.task_id == task_id,
                    TaskLease.lease_token == lease_token,
                    ~select(TaskDispatch.task_id).where(TaskDispatch.task_id == task_id).exists(),
                )
                .execution_options(synchronize_session=False)
            )
            if not removed.rowcount:
                session.rollback()
                return False
            task = session.get(TaskModel, task_id)
            if task is not None:
                run, tasks = self._recover_run(session, task)
                reconcile = self._reconcile_run(session, run, tasks, _utcnow())
            session.commit()
        if reconcile:
            emit_task_event(self._session_factory, task_id, "task_reconciliation_required")
        return True

    def _recover_run(
        self, session: Session, task: TaskModel
    ) -> tuple[WorkflowRun, list[TaskModel]]:
        from apflow.core.execution.runs import new_run_record
        from apflow.core.execution.error_info import execution_error_info

        root, visited = task, {task.id}
        while root.parent_id is not None:
            parent = session.get(TaskModel, root.parent_id)
            if parent is None or parent.id in visited:
                raise ValueError("Interrupted task has an invalid structural ancestry")
            root = parent
            visited.add(root.id)
        session.execute(
            update(TaskModel).where(TaskModel.id == root.id).values(updated_at=TaskModel.updated_at)
        )
        descendants = (
            select(TaskModel.id)
            .where(TaskModel.id == root.id)
            .cte(name="recovery_tree", recursive=True)
        )
        descendants = descendants.union_all(
            select(TaskModel.id).where(TaskModel.parent_id == descendants.c.id)
        )
        tasks = list(
            session.scalars(
                select(TaskModel).where(TaskModel.id.in_(select(descendants.c.id)))
            ).all()
        )
        run = session.get(WorkflowRun, root.run_id or root.id)
        if run is None:
            for node in tasks:
                if (
                    node.status == "in_progress"
                    and (node.error_info or {}).get("delivery_state") != "unknown"
                ):
                    node.error_info = dict(
                        execution_error_info(
                            RuntimeError("Execution lease expired without a confirmed outcome"),
                            task_id=node.id,
                            attempt_id=node.attempt_id or 0,
                            phase="lease_recovery",
                        )
                    )
            run = new_run_record(root, tasks)
            session.add(run)
            session.flush()
            session.execute(
                update(TaskModel)
                .where(TaskModel.id.in_([node.id for node in tasks]), TaskModel.run_id.is_(None))
                .values(run_id=run.id)
                .execution_options(synchronize_session="fetch")
            )
        return run, tasks

    def _reconcile_run(
        self, session: Session, run: WorkflowRun, tasks: list[TaskModel], now: datetime
    ) -> bool:
        invocations = list(
            session.scalars(
                select(ExecutionAttempt).where(
                    or_(
                        ExecutionAttempt.run_id == run.id,
                        ExecutionAttempt.task_id.in_([node.id for node in tasks]),
                    )
                )
            ).all()
        )
        interrupted = [
            invocation for invocation in invocations if invocation.status in {"running", "unknown"}
        ]
        for invocation in invocations:
            if invocation.run_id is None:
                invocation.run_id = run.id
        started = bool(invocations) or any(
            node.started_at is not None or (node.status != "pending" and node.origin_type != "link")
            for node in tasks
        )
        if run.status in {"accepted", "running"}:
            run.status = "reconciliation_required" if started else "accepted"
            run.completed_at = now if started else None
            if not started:
                run.started_at = None
        for invocation in interrupted:
            session.execute(
                update(ExecutionAttempt)
                .where(ExecutionAttempt.id == invocation.id, ExecutionAttempt.status == "running")
                .values(
                    status="unknown",
                    completed_at=func.coalesce(ExecutionAttempt.completed_at, now),
                )
                .execution_options(synchronize_session=False)
            )
        if run.status == "reconciliation_required" or interrupted:
            self._hold_unknown_reservations(session, [node.id for node in tasks])
        return run.status == "reconciliation_required"

    def _hold_unknown_reservations(self, session: Session, task_ids: list[str]) -> None:
        held = session.execute(
            update(BudgetReservation)
            .where(BudgetReservation.task_id.in_(task_ids), BudgetReservation.state == "reserved")
            .values(state="unknown", actual_tokens=None)
            .returning(BudgetReservation.scope_id, BudgetReservation.reserved_tokens)
            .execution_options(synchronize_session=False)
        ).all()
        for scope_id, tokens in held:
            session.execute(
                update(BudgetAccount)
                .where(BudgetAccount.scope_id == scope_id)
                .values(
                    reserved_tokens=BudgetAccount.reserved_tokens - tokens,
                    unknown_tokens=BudgetAccount.unknown_tokens + tokens,
                )
                .execution_options(synchronize_session=False)
            )
