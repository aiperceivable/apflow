"""Lease manager service for distributed task execution.

Manages task lease acquisition, renewal, release, and cleanup
to ensure exactly-once execution semantics across worker nodes.
"""

import uuid
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from apflow.core.distributed.config import (
    DistributedConfig,
    as_utc,
    is_postgresql,
    utcnow as _utcnow,
)
from apflow.core.distributed.events import emit_task_event
from apflow.core.storage.sqlalchemy.models import TaskLease, TaskModel
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
            if is_postgresql(session):
                return self._acquire_lease_postgresql(session, task_id, node_id)
            return self._acquire_lease_default(session, task_id, node_id)

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
        """Delete expired leases and revert their tasks to pending status.

        Returns a list of task_ids whose leases were cleaned up.
        """
        with self._session_factory() as session:
            now = _utcnow()
            expired_leases = session.query(TaskLease).filter(TaskLease.expires_at <= now).all()

            cleaned_task_ids: list[str] = []
            for lease in expired_leases:
                task = session.get(TaskModel, lease.task_id)
                if task is not None and task.status == "in_progress":
                    task.status = "pending"
                    # Bump attempt_id so the reassigned execution computes a fresh
                    # idempotency key rather than colliding with the timed-out
                    # attempt's stored (and possibly failed) result.
                    task.attempt_id = (task.attempt_id or 0) + 1
                session.delete(lease)
                cleaned_task_ids.append(lease.task_id)
                logger.info("Cleaned up expired lease for task %s", lease.task_id)

            session.commit()

        for tid in cleaned_task_ids:
            emit_task_event(self._session_factory, tid, "task_reassigned")

        return cleaned_task_ids
