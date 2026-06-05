"""Tests for IdempotencyManager key generation and result caching."""

from apflow.core.distributed.idempotency import IdempotencyManager
from apflow.core.storage.sqlalchemy.models import ExecutionIdempotency


class TestIdempotencyKeyGeneration:
    """Test IdempotencyManager.generate_key static method."""

    def test_generate_key_deterministic(self) -> None:
        """Same (task_id, attempt_id, inputs) produces same key."""
        key1 = IdempotencyManager.generate_key("task-1", 0, {"x": 1})
        key2 = IdempotencyManager.generate_key("task-1", 0, {"x": 1})
        assert key1 == key2

    def test_generate_key_different_inputs_differ(self) -> None:
        """Different inputs produce different keys."""
        key1 = IdempotencyManager.generate_key("task-1", 0, {"x": 1})
        key2 = IdempotencyManager.generate_key("task-1", 0, {"x": 2})
        assert key1 != key2

    def test_generate_key_different_attempt_differ(self) -> None:
        """Different attempt_id produces different keys."""
        key1 = IdempotencyManager.generate_key("task-1", 0, {"x": 1})
        key2 = IdempotencyManager.generate_key("task-1", 1, {"x": 1})
        assert key1 != key2

    def test_generate_key_different_task_differ(self) -> None:
        """Different task_id produces different keys."""
        key1 = IdempotencyManager.generate_key("task-1", 0, {"x": 1})
        key2 = IdempotencyManager.generate_key("task-2", 0, {"x": 1})
        assert key1 != key2

    def test_generate_key_none_inputs(self) -> None:
        """None inputs produces consistent key."""
        key1 = IdempotencyManager.generate_key("task-1", 0, None)
        key2 = IdempotencyManager.generate_key("task-1", 0, None)
        assert key1 == key2

    def test_generate_key_none_vs_empty_dict_differ(self) -> None:
        """None inputs and empty dict inputs produce different keys."""
        key_none = IdempotencyManager.generate_key("task-1", 0, None)
        key_empty = IdempotencyManager.generate_key("task-1", 0, {})
        assert key_none != key_empty

    def test_generate_key_dict_order_independent(self) -> None:
        """Key is the same regardless of dict key insertion order."""
        key1 = IdempotencyManager.generate_key("task-1", 0, {"a": 1, "b": 2})
        key2 = IdempotencyManager.generate_key("task-1", 0, {"b": 2, "a": 1})
        assert key1 == key2

    def test_generate_key_returns_hex_string(self) -> None:
        """Generated key is a valid hex string (SHA-256)."""
        key = IdempotencyManager.generate_key("task-1", 0, {"x": 1})
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)


class TestIdempotencyManager:
    """Test IdempotencyManager DB operations."""

    def test_check_cached_result_miss(self, session_factory) -> None:
        """check_cached_result returns (False, None) for unknown key."""
        manager = IdempotencyManager(session_factory)
        is_cached, result = manager.check_cached_result("nonexistent-key")
        assert is_cached is False
        assert result is None

    def test_store_and_check_result(self, session_factory) -> None:
        """store_result then check_cached_result returns (True, result)."""
        manager = IdempotencyManager(session_factory)
        key = IdempotencyManager.generate_key("task-1", 0, {"x": 1})
        expected_result = {"output": "success", "value": 42}

        manager.store_result(
            task_id="task-1",
            attempt_id=0,
            idempotency_key=key,
            result=expected_result,
        )

        is_cached, result = manager.check_cached_result(key)
        assert is_cached is True
        assert result == expected_result

    def test_store_failure_status(self, session_factory) -> None:
        """store_failure sets status to 'failed'."""
        manager = IdempotencyManager(session_factory)
        key = IdempotencyManager.generate_key("task-1", 0, {"x": 1})
        error_info = {"error": "timeout", "code": 504}

        manager.store_failure(
            task_id="task-1",
            attempt_id=0,
            idempotency_key=key,
            error=error_info,
        )

        # Verify directly via session
        session = session_factory()
        try:
            record = (
                session.query(ExecutionIdempotency)
                .filter(ExecutionIdempotency.idempotency_key == key)
                .first()
            )
            assert record is not None
            assert record.status == "failed"
            assert record.result == error_info
        finally:
            session.close()

    def test_check_cached_failure_returns_false(self, session_factory) -> None:
        """check_cached_result returns (False, None) for failed entries."""
        manager = IdempotencyManager(session_factory)
        key = IdempotencyManager.generate_key("task-1", 0, {"x": 1})

        manager.store_failure(
            task_id="task-1",
            attempt_id=0,
            idempotency_key=key,
            error={"error": "timeout"},
        )

        is_cached, result = manager.check_cached_result(key)
        assert is_cached is False
        assert result is None

    def test_store_result_with_custom_status(self, session_factory) -> None:
        """store_result can store with a custom status like 'pending'."""
        manager = IdempotencyManager(session_factory)
        key = IdempotencyManager.generate_key("task-1", 0, {"x": 1})

        manager.store_result(
            task_id="task-1",
            attempt_id=0,
            idempotency_key=key,
            result={"partial": True},
            status="pending",
        )

        # Pending status should not be returned as cached
        is_cached, result = manager.check_cached_result(key)
        assert is_cached is False
        assert result is None


class TestStoreResultReconcile:
    """store_result reconciles a stale failure to success on key conflict."""

    def test_reconciles_failed_to_completed_on_conflict(self) -> None:
        from unittest.mock import MagicMock
        from sqlalchemy.exc import IntegrityError

        existing = MagicMock()
        existing.status = "failed"
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        # First commit (insert) conflicts; second commit (reconcile) succeeds.
        session.commit.side_effect = [IntegrityError("stmt", {}, Exception("dup")), None]
        session.query.return_value.filter.return_value.first.return_value = existing
        factory = MagicMock(return_value=session)

        mgr = IdempotencyManager(factory)
        mgr.store_result("t1", 0, "key", {"ok": True}, status="completed")

        assert existing.status == "completed"
        assert existing.result == {"ok": True}

    def test_does_not_downgrade_existing_completed(self) -> None:
        from unittest.mock import MagicMock
        from sqlalchemy.exc import IntegrityError

        existing = MagicMock()
        existing.status = "completed"
        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.commit.side_effect = [IntegrityError("stmt", {}, Exception("dup"))]
        session.query.return_value.filter.return_value.first.return_value = existing
        factory = MagicMock(return_value=session)

        mgr = IdempotencyManager(factory)
        # A losing failed-writer must not overwrite an existing completed row.
        mgr.store_result("t1", 0, "key", {"error": "x"}, status="failed")

        assert existing.status == "completed"
