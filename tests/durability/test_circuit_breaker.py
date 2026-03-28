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
