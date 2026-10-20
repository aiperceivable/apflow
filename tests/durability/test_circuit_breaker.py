"""Tests for circuit breaker module"""

import time

import pytest
from apflow.durability.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitState,
)


class TestCircuitBreakerStates:
    def test_initial_state_closed(self):
        cb = CircuitBreaker("exec1", CircuitBreakerConfig())
        assert cb.state == CircuitState.CLOSED

    def test_can_execute_when_closed(self):
        cb = CircuitBreaker("exec1", CircuitBreakerConfig())
        assert cb.can_execute()

    def test_opens_after_threshold(self):
        cb = CircuitBreaker("exec1", CircuitBreakerConfig(failure_threshold=3))
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_blocks_when_open(self):
        cb = CircuitBreaker("exec1", CircuitBreakerConfig(failure_threshold=1))
        cb.record_failure()
        assert not cb.can_execute()

    def test_half_open_after_timeout(self):
        cb = CircuitBreaker(
            "exec1", CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=1.0)
        )
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(1.1)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_allows_limited_attempts(self):
        cb = CircuitBreaker(
            "exec1",
            CircuitBreakerConfig(
                failure_threshold=1, reset_timeout_seconds=1.0, half_open_max_attempts=2
            ),
        )
        cb.record_failure()
        time.sleep(1.1)
        assert cb.can_execute()  # 1st
        assert cb.can_execute()  # 2nd
        assert not cb.can_execute()  # 3rd blocked

    def test_half_open_success_closes(self):
        cb = CircuitBreaker(
            "exec1", CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=1.0)
        )
        cb.record_failure()
        time.sleep(1.1)
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.can_execute()

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker(
            "exec1", CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=1.0)
        )
        cb.record_failure()
        time.sleep(1.1)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker("exec1", CircuitBreakerConfig(failure_threshold=3))
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED  # Count reset

    def test_force_reset(self):
        cb = CircuitBreaker("exec1", CircuitBreakerConfig(failure_threshold=1))
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED

    def test_empty_id_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            CircuitBreaker("", CircuitBreakerConfig())

    def test_half_open_self_heals_when_test_outcome_never_recorded(self):
        """Regression: a HALF_OPEN test attempt whose outcome is never
        recorded (e.g. a cancelled call that raises before either
        record_success() or record_failure() runs) previously wedged the
        breaker in HALF_OPEN forever — half_open_attempts stayed at its cap
        with nothing left to trigger a state transition. It must instead
        allow a fresh attempt once reset_timeout_seconds has elapsed again,
        the same way a stale OPEN circuit does. (Review CRITICAL #55)
        """
        cb = CircuitBreaker(
            "exec1",
            CircuitBreakerConfig(
                failure_threshold=1, reset_timeout_seconds=1.0, half_open_max_attempts=1
            ),
        )
        cb.record_failure()
        time.sleep(1.1)
        assert cb.state == CircuitState.HALF_OPEN

        # Consume the single allowed test attempt, but never report its
        # outcome (simulating a cancelled call).
        assert cb.can_execute() is True
        assert cb.can_execute() is False  # attempts exhausted, no outcome recorded

        # Without self-healing this would stay False forever.
        time.sleep(1.1)
        assert cb.can_execute() is True


class TestCircuitBreakerConfig:
    def test_threshold_zero_raises(self):
        with pytest.raises(ValueError):
            CircuitBreakerConfig(failure_threshold=0)

    def test_threshold_1001_raises(self):
        with pytest.raises(ValueError):
            CircuitBreakerConfig(failure_threshold=1001)

    def test_timeout_too_low_raises(self):
        with pytest.raises(ValueError):
            CircuitBreakerConfig(reset_timeout_seconds=0.9)

    def test_timeout_too_high_raises(self):
        with pytest.raises(ValueError):
            CircuitBreakerConfig(reset_timeout_seconds=86400.1)


class TestCircuitBreakerRegistry:
    def test_get_creates(self):
        registry = CircuitBreakerRegistry()
        cb = registry.get("exec1")
        assert cb.state == CircuitState.CLOSED

    def test_get_returns_same(self):
        registry = CircuitBreakerRegistry()
        cb1 = registry.get("exec1")
        cb2 = registry.get("exec1")
        assert cb1 is cb2

    def test_reset_all(self):
        registry = CircuitBreakerRegistry()
        cb = registry.get("exec1", CircuitBreakerConfig(failure_threshold=1))
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        registry.reset_all()
        assert cb.state == CircuitState.CLOSED


def test_open_breaker_ignores_success_from_older_inflight_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apflow.durability.circuit_breaker.time.monotonic", lambda: 100.0)
    breaker = CircuitBreaker("shared", CircuitBreakerConfig(failure_threshold=1))
    assert breaker.can_execute()
    assert breaker.can_execute()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.state == CircuitState.OPEN
    assert not breaker.can_execute()


def test_half_open_probe_ignores_success_from_previous_admission_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextvars import copy_context

    now = [100.0]
    monkeypatch.setattr("apflow.durability.circuit_breaker.time.monotonic", lambda: now[0])
    breaker = CircuitBreaker("shared", CircuitBreakerConfig(failure_threshold=1))
    old_context = copy_context()
    assert old_context.run(breaker.can_execute)
    breaker.record_failure()
    now[0] += 60.0
    assert breaker.can_execute()
    old_context.run(breaker.record_success)
    assert breaker.state == CircuitState.HALF_OPEN
    assert not breaker.can_execute()
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED
