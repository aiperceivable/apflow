"""Idempotency key generation and result caching for distributed execution."""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from apflow.core.storage.sqlalchemy.models import ExecutionIdempotency
from apflow.logger import get_logger

logger = get_logger(__name__)


class IdempotencyManager:
    """Manages idempotency keys and cached execution results.

    Ensures that retried task executions with the same inputs produce
    the same result without re-executing. Uses SHA-256 hashing of
    canonical JSON to generate deterministic keys.
    """

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    @staticmethod
    def generate_key(task_id: str, attempt_id: int, inputs: dict[str, Any] | None) -> str:
        """Generate deterministic idempotency key from task_id + attempt_id + inputs.

        Uses SHA-256 hash of canonical JSON (sorted keys, no whitespace)
        to produce a consistent key regardless of dict ordering. ``inputs`` is an
        optional discriminator: pass the task's inputs so two executions with the
        same (task_id, attempt_id) but different inputs key differently; pass None
        to key purely on (task_id, attempt_id).
        """
        canonical = json.dumps(
            {"task_id": task_id, "attempt_id": attempt_id, "inputs": inputs},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def check_cached_result(self, idempotency_key: str) -> tuple[bool, dict[str, Any] | None]:
        """Check if a result is cached for this key.

        Returns (is_cached, result). is_cached=True only if status='completed'.
        """
        with self._session_factory() as session:
            record = (
                session.query(ExecutionIdempotency)
                .filter(ExecutionIdempotency.idempotency_key == idempotency_key)
                .first()
            )
            if record is None:
                return False, None
            if cast(str, record.status) != "completed":
                return False, None
            return True, cast(dict[str, Any] | None, record.result)

    def store_result(
        self,
        task_id: str,
        attempt_id: int,
        idempotency_key: str,
        result: dict[str, Any],
        status: str = "completed",
    ) -> None:
        """Store execution result for idempotency key."""
        with self._session_factory() as session:
            record = ExecutionIdempotency(
                task_id=task_id,
                attempt_id=attempt_id,
                idempotency_key=idempotency_key,
                result=result,
                status=status,
            )
            session.add(record)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = (
                    session.query(ExecutionIdempotency)
                    .filter(ExecutionIdempotency.idempotency_key == idempotency_key)
                    .first()
                )
                existing_status = cast(str, existing.status) if existing is not None else "unknown"
                # Reconcile a stale failure: if a prior row recorded failure but this
                # attempt completed, upgrade it so the cache reflects the real success
                # instead of permanently returning the earlier failure.
                if (
                    existing is not None
                    and status == "completed"
                    and existing_status != "completed"
                ):
                    existing.status = status  # type: ignore[assignment]
                    existing.result = result  # type: ignore[assignment]
                    session.commit()
                    logger.info(
                        "Reconciled idempotency row for task=%s attempt=%d: %s -> completed",
                        task_id,
                        attempt_id,
                        existing_status,
                    )
                    return
                logger.info(
                    "Idempotency result already stored for task=%s attempt=%d "
                    "(concurrent write, existing status=%s)",
                    task_id,
                    attempt_id,
                    existing_status,
                )
                return
            logger.info(
                "Stored idempotency result for task=%s attempt=%d status=%s",
                task_id,
                attempt_id,
                status,
            )

    def store_failure(
        self,
        task_id: str,
        attempt_id: int,
        idempotency_key: str,
        error: dict[str, Any],
    ) -> None:
        """Store execution failure for idempotency key."""
        self.store_result(task_id, attempt_id, idempotency_key, error, status="failed")
