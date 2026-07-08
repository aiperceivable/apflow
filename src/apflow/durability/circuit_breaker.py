"""
Circuit breaker pattern for executor fault isolation.
"""

import time
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Dict, Optional

from apflow.logger import get_logger

logger = get_logger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    """Configuration for a circuit breaker.

    Args:
        failure_threshold: Consecutive failures to trigger OPEN. Must be 1-1000.
        reset_timeout_seconds: Seconds before OPEN -> HALF_OPEN. Must be 1.0-86400.0.
        half_open_max_attempts: Attempts allowed in HALF_OPEN. Must be 1-10.
    """

    failure_threshold: int = 5
    reset_timeout_seconds: float = 60.0
    half_open_max_attempts: int = 1

    def __post_init__(self) -> None:
        if not (1 <= self.failure_threshold <= 1000):
            raise ValueError(f"failure_threshold must be 1-1000, got {self.failure_threshold}")
        if not (1.0 <= self.reset_timeout_seconds <= 86400.0):
            raise ValueError(
                f"reset_timeout_seconds must be 1.0-86400.0, got {self.reset_timeout_seconds}"
            )
        if not (1 <= self.half_open_max_attempts <= 10):
            raise ValueError(
                f"half_open_max_attempts must be 1-10, got {self.half_open_max_attempts}"
            )


class CircuitBreaker:
    """Circuit breaker for a single executor.

    State machine:
    - CLOSED: Normal operation. Failures increment counter.
    - OPEN: All executions blocked. Transitions to HALF_OPEN after timeout.
    - HALF_OPEN: Limited test executions allowed. Success -> CLOSED, Failure -> OPEN.
    """

    def __init__(self, executor_id: str, config: CircuitBreakerConfig) -> None:
        if not executor_id:
            raise ValueError("executor_id must be non-empty")
        self._executor_id = executor_id
        self._config = config
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._half_open_attempts = 0
        self._half_open_entered_at: float = 0.0
        self._lock = Lock()

    def _check_and_transition_state(self) -> None:
        """Must be called with self._lock held.

        Handles OPEN -> HALF_OPEN after the reset timeout, and HALF_OPEN
        self-healing: if a test attempt's outcome is never recorded (e.g. a
        cancelled call raises before either record_success() or
        record_failure() runs), half_open_attempts stays at its cap forever
        and can_execute() would otherwise return False indefinitely, with
        nothing left to trigger the HALF_OPEN -> OPEN transition that
        normally restarts the reset_timeout_seconds countdown. Treating a
        stale HALF_OPEN the same way as a stale OPEN — allow a fresh test
        attempt after the same timeout — keeps the breaker self-healing.
        """
        now = time.monotonic()
        if self._state == CircuitState.OPEN:
            elapsed = now - self._last_failure_time
            if elapsed >= self._config.reset_timeout_seconds:
                self._state = CircuitState.HALF_OPEN
                self._half_open_attempts = 0
                self._half_open_entered_at = now
                logger.info(
                    f"Circuit breaker {self._executor_id}: OPEN -> HALF_OPEN "
                    f"(after {elapsed:.1f}s)"
                )
        elif self._state == CircuitState.HALF_OPEN:
            stale = now - self._half_open_entered_at
            if stale >= self._config.reset_timeout_seconds:
                logger.warning(
                    f"Circuit breaker {self._executor_id}: HALF_OPEN test attempt "
                    f"outcome never recorded after {stale:.1f}s; allowing a fresh attempt"
                )
                self._half_open_attempts = 0
                self._half_open_entered_at = now

    @property
    def state(self) -> CircuitState:
        """Current state, with automatic OPEN -> HALF_OPEN transition after timeout."""
        with self._lock:
            self._check_and_transition_state()
            return self._state

    def can_execute(self) -> bool:
        """Whether execution is currently allowed."""
        with self._lock:
            self._check_and_transition_state()

            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_attempts < self._config.half_open_max_attempts:
                    self._half_open_attempts += 1
                    return True
                return False
            return False  # OPEN

    def record_success(self) -> None:
        """Record a successful execution."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.info(f"Circuit breaker {self._executor_id}: HALF_OPEN -> CLOSED")
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_attempts = 0

    def record_failure(self) -> None:
        """Record a failed execution."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._half_open_attempts = 0
                logger.warning(
                    f"Circuit breaker {self._executor_id}: HALF_OPEN -> OPEN (failure in test)"
                )
            elif self._failure_count >= self._config.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    f"Circuit breaker {self._executor_id}: CLOSED -> OPEN "
                    f"(failures: {self._failure_count}/{self._config.failure_threshold})"
                )

    def reset(self) -> None:
        """Force-reset to CLOSED state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_attempts = 0
            logger.info(f"Circuit breaker {self._executor_id}: force-reset to CLOSED")


class CircuitBreakerRegistry:
    """Thread-safe registry of circuit breakers, keyed by executor ID."""

    def __init__(self, default_config: Optional[CircuitBreakerConfig] = None) -> None:
        self._default_config = default_config or CircuitBreakerConfig()
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = Lock()

    def get(
        self, executor_id: str, config: Optional[CircuitBreakerConfig] = None
    ) -> CircuitBreaker:
        """Get or create a circuit breaker for an executor."""
        with self._lock:
            if executor_id not in self._breakers:
                self._breakers[executor_id] = CircuitBreaker(
                    executor_id, config or self._default_config
                )
            return self._breakers[executor_id]

    def reset_all(self) -> None:
        """Reset all circuit breakers to CLOSED."""
        with self._lock:
            for cb in self._breakers.values():
                cb.reset()
