---
description: Feature specification for apflow checkpointing, retry, circuit breakers, and durable task execution.
---

# Feature Spec: Durable Execution (F-003)

**Feature ID:** F-003
**Priority:** P0
**Phase:** Phase 2 (0.20.0-alpha.2)
**Tech Design Reference:** Section 4.4

---

## Purpose

Enable checkpoint/resume for long-running agent tasks so that failures do not lose progress. Provides retry with configurable backoff strategies and circuit breaker pattern to prevent repeated execution of consistently failing executors.

---

## File Changes

### New Files

**`src/apflow/durability/__init__.py`**

```python
"""Durable execution: checkpoint, retry, circuit breaker."""

from apflow.durability.checkpoint import CheckpointManager
from apflow.durability.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitState,
)
from apflow.durability.retry import BackoffStrategy, RetryManager, RetryPolicy

__all__ = [
    "CheckpointManager",
    "RetryPolicy",
    "RetryManager",
    "BackoffStrategy",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerRegistry",
    "CircuitState",
]
```

**`src/apflow/durability/checkpoint.py`** -- CheckpointManager class.
**`src/apflow/durability/retry.py`** -- BackoffStrategy enum, RetryPolicy dataclass, RetryManager class.
**`src/apflow/durability/circuit_breaker.py`** -- CircuitState enum, CircuitBreakerConfig dataclass, CircuitBreaker class, CircuitBreakerRegistry class.
**`src/apflow/core/storage/migrations/004_add_durability_and_governance.py`** -- Migration for new table and columns.

### Modified Files

**`src/apflow/core/interfaces/executable_task.py`**

Add three optional methods after `cancel()`:

```python
def supports_checkpoint(self) -> bool:
    """Whether this task supports checkpoint/resume. Default: False."""
    return False

def get_checkpoint(self) -> Optional[Dict[str, Any]]:
    """Serialize current execution state. Default: None."""
    return None

async def resume_from_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
    """Restore state from checkpoint. Default: no-op."""
    pass
```

These are concrete methods (not abstract), so existing executors are not affected.

**`src/apflow/core/storage/sqlalchemy/models.py`**

Add to `TaskModel` class after the distributed execution fields:

```python
# === Durability Fields (F-003) ===
checkpoint_at = Column(DateTime(timezone=True), nullable=True)
resume_from = Column(String(255), nullable=True)
attempt_count = Column(Integer, default=0)
max_attempts = Column(Integer, default=3)
backoff_strategy = Column(String(20), default="exponential")
backoff_base_seconds = Column(Numeric(10, 2), default=1.0)
```

Add new model class:

```python
class TaskCheckpointModel(Base):
    __tablename__ = "task_checkpoints"

    id = Column(String(255), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(String(255), ForeignKey(f"{TASK_TABLE_NAME}.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    checkpoint_data = Column(Text, nullable=False)
    step_name = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
```

Update `to_dict()` to include new fields.

**`src/apflow/core/execution/task_manager.py`**

Add integration points:

1. In `__init__()`: Accept optional `checkpoint_manager`, `retry_manager`, `circuit_breaker_registry` parameters.
2. In the method that executes a single task (around line 1000-1020):
   - Before executor creation: check circuit breaker (`can_execute()`).
   - Before executor creation: load checkpoint (`load_checkpoint(task_id)`).
   - If checkpoint exists: inject into executor inputs as `final_inputs["_checkpoint"] = checkpoint_data`.
   - Wrap `execute()` call with `retry_manager.execute_with_retry()`.
   - After success: `record_success()` on circuit breaker, delete old checkpoints.
   - After failure: `record_failure()` on circuit breaker, update `attempt_count`.

### Test Files

```
tests/durability/__init__.py
tests/durability/test_checkpoint.py
tests/durability/test_retry.py
tests/durability/test_circuit_breaker.py
tests/durability/test_integration.py
```

---

## Method Signatures

### CheckpointManager

```python
class CheckpointManager:
    def __init__(self, db: Session | AsyncSession) -> None:
        """Args: db (sync OR async SQLAlchemy session, not None). Raises: TypeError if None.

        Methods are async and support both session types (the live execution
        session is an AsyncSession); the sync path runs without awaiting the driver.
        """

    async def save_checkpoint(
        self,
        task_id: str,          # Non-empty string
        data: dict[str, Any],  # JSON-serializable dict
        step_name: Optional[str] = None,
    ) -> str:
        """Save checkpoint. Returns checkpoint_id (UUID).
        Logic:
        1. Validate task_id non-empty.
        2. Validate data is dict.
        3. json.dumps(data) to verify serializability. Raise ValueError if fails.
        4. Generate UUID for checkpoint_id.
        5. INSERT INTO task_checkpoints via ORM.
        6. UPDATE task checkpoint_at, resume_from via ORM query.
        7. Return checkpoint_id.
        """

    async def load_checkpoint(self, task_id: str) -> Optional[dict[str, Any]]:
        """Load latest checkpoint. Returns None if no checkpoint exists.
        Logic:
        1. Validate task_id non-empty.
        2. SELECT checkpoint_data FROM task_checkpoints WHERE task_id ORDER BY created_at DESC LIMIT 1.
        3. If no row, return None.
        4. json.loads(row.checkpoint_data).
        5. Return parsed dict.
        """

    async def delete_checkpoints(self, task_id: str) -> int:
        """Delete all checkpoints for task. Returns count deleted.
        Logic:
        1. Validate task_id non-empty.
        2. DELETE FROM task_checkpoints WHERE task_id.
        3. Return rowcount.
        """
```

### RetryPolicy

```python
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3           # Constraint: 1 <= x <= 100
    backoff_strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    backoff_base_seconds: float = 1.0    # Constraint: 0.1 <= x <= 3600.0
    backoff_max_seconds: float = 300.0   # Constraint: >= backoff_base_seconds, <= 86400.0
    jitter: bool = True

    def calculate_delay(self, attempt: int) -> float:
        """Calculate delay for retry attempt (0-indexed).
        Logic:
        - FIXED: delay = backoff_base_seconds
        - EXPONENTIAL: delay = backoff_base_seconds * (2 ** attempt)
        - LINEAR: delay = backoff_base_seconds * (attempt + 1)
        - Cap at backoff_max_seconds.
        - If jitter: add random uniform [-25%, +25%] of delay.
        - Floor at 0.0.
        Raises: ValueError if attempt < 0.
        """
```

### RetryManager

```python
class RetryManager:
    def __init__(self, checkpoint_manager: Optional[CheckpointManager] = None) -> None: ...

    async def execute_with_retry(
        self,
        task_id: str,
        policy: RetryPolicy,
        execute_fn: Callable[..., Awaitable[Dict[str, Any]]],
        on_retry: Optional[Callable[[str, int, Exception], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """Execute with retry.
        Logic:
        1. For attempt in range(policy.max_attempts):
           a. Try execute_fn().
           b. On success: log if attempt > 0, return result.
           c. On exception:
              i. Log warning.
              ii. If last attempt, break and raise.
              iii. Call on_retry callback if provided (checkpoint saving is the
                   caller's responsibility; TaskManager injects an _on_retry closure
                   that calls CheckpointManager.save_checkpoint when the executor
                   implements get_checkpoint()).
              iv. Sleep for calculate_delay(attempt).
        2. Raise last exception.
        """
```

### CircuitBreaker

```python
class CircuitBreaker:
    def __init__(self, executor_id: str, config: CircuitBreakerConfig) -> None:
        """Args: executor_id (non-empty), config. Raises: ValueError if empty id."""

    @property
    def state(self) -> CircuitState:
        """Current state with auto-transition from OPEN to HALF_OPEN after timeout."""

    def can_execute(self) -> bool:
        """CLOSED: True. HALF_OPEN: True if attempts < max. OPEN: False."""

    def record_success(self) -> None:
        """Reset failure count, set state to CLOSED."""

    def record_failure(self) -> None:
        """Increment failure count. If HALF_OPEN: reopen. If failures >= threshold: open."""

    def reset(self) -> None:
        """Force-reset to CLOSED."""
```

### CircuitBreakerConfig

```python
@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5        # Constraint: 1 <= x <= 1000
    reset_timeout_seconds: float = 60.0  # Constraint: 1.0 <= x <= 86400.0
    half_open_max_attempts: int = 1   # Constraint: 1 <= x <= 10
```

### CircuitBreakerRegistry

```python
class CircuitBreakerRegistry:
    def __init__(self, default_config: Optional[CircuitBreakerConfig] = None) -> None: ...

    def get(self, executor_id: str, config: Optional[CircuitBreakerConfig] = None) -> CircuitBreaker:
        """Get or create circuit breaker for executor. Thread-safe."""

    def reset_all(self) -> None:
        """Reset all circuit breakers to CLOSED."""
```

---

## Data Models

### TaskModel Additions

| Field | SQLAlchemy Type | Python Type | Default | Validation |
|---|---|---|---|---|
| `checkpoint_at` | `DateTime(timezone=True)` | `Optional[datetime]` | `None` | nullable |
| `resume_from` | `String(255)` | `Optional[str]` | `None` | nullable |
| `attempt_count` | `Integer` | `int` | `0` | `>= 0` |
| `max_attempts` | `Integer` | `int` | `3` | `1 <= x <= 100` |
| `backoff_strategy` | `String(20)` | `Optional[str]` | `"exponential"` | enum: fixed, exponential, linear |
| `backoff_base_seconds` | `Numeric(10,2)` | `float` | `1.0` | `0.1 <= x <= 3600.0` |

### TaskCheckpointModel (New Table)

| Column | SQLAlchemy Type | Constraint |
|---|---|---|
| `id` | `String(255)` | PRIMARY KEY, default uuid4 |
| `task_id` | `String(255)` | NOT NULL, FK apflow_tasks.id ON DELETE CASCADE, INDEX |
| `checkpoint_data` | `Text` | NOT NULL |
| `step_name` | `String(255)` | nullable |
| `created_at` | `DateTime(timezone=True)` | NOT NULL, default now() |

---

## Test Requirements

### Unit Tests: `tests/durability/test_checkpoint.py`

```python
class TestSaveCheckpoint:
    async def test_save_returns_uuid(self, checkpoint_mgr, task_id):
        """Save returns a valid UUID checkpoint_id."""

    async def test_save_empty_task_id_raises(self, checkpoint_mgr):
        """Empty task_id raises ValueError."""

    async def test_save_non_dict_raises(self, checkpoint_mgr, task_id):
        """Non-dict data raises TypeError."""

    async def test_save_non_serializable_raises(self, checkpoint_mgr, task_id):
        """Non-JSON-serializable data raises ValueError."""

    async def test_save_with_step_name(self, checkpoint_mgr, task_id):
        """step_name is stored and retrievable."""

    async def test_save_base64_binary(self, checkpoint_mgr, task_id):
        """Binary data encoded as base64 string is accepted."""
        import base64
        data = {"binary": base64.b64encode(b"hello").decode()}
        # Should succeed

class TestLoadCheckpoint:
    async def test_load_returns_latest(self, checkpoint_mgr, task_id):
        """Load returns the most recently saved checkpoint."""
        # Save checkpoint A, then B. Load should return B.

    async def test_load_none_when_empty(self, checkpoint_mgr, task_id):
        """Load returns None when no checkpoint exists."""

    async def test_load_empty_task_id_raises(self, checkpoint_mgr):
        """Empty task_id raises ValueError."""

class TestDeleteCheckpoints:
    async def test_delete_returns_count(self, checkpoint_mgr, task_id):
        """Delete removes all checkpoints for task. Returns count."""
        # Save 3 checkpoints. Delete. Verify count=3. Load returns None.

    async def test_delete_empty_task_id_raises(self, checkpoint_mgr):
        """Empty task_id raises ValueError."""

    async def test_delete_nonexistent_returns_zero(self, checkpoint_mgr):
        """Delete on a task with no checkpoints returns 0."""

class TestCheckpointManagerInit:
    def test_none_db_raises(self):
        """Passing None as db session raises TypeError."""
```

### Unit Tests: `tests/durability/test_retry.py`

```python
def test_retry_policy_defaults():
    """Default values: max_attempts=3, exponential, base=1.0, max=300.0, jitter=True."""

def test_retry_policy_max_attempts_boundary():
    """max_attempts=0 raises, 1 is valid (no retry), 100 is valid, 101 raises."""

def test_retry_policy_backoff_base_boundary():
    """backoff_base=0.09 raises, 0.1 valid, 3600.0 valid, 3600.1 raises."""

def test_retry_policy_max_less_than_base_raises():
    """backoff_max < backoff_base raises ValueError."""

def test_calculate_delay_fixed():
    """Fixed: delay is always backoff_base_seconds (ignoring jitter)."""
    policy = RetryPolicy(backoff_strategy=BackoffStrategy.FIXED, backoff_base_seconds=2.0, jitter=False)
    assert policy.calculate_delay(0) == 2.0
    assert policy.calculate_delay(5) == 2.0

def test_calculate_delay_exponential():
    """Exponential: delay = base * 2^attempt (no jitter)."""
    policy = RetryPolicy(backoff_strategy=BackoffStrategy.EXPONENTIAL, backoff_base_seconds=1.0, jitter=False)
    assert policy.calculate_delay(0) == 1.0
    assert policy.calculate_delay(1) == 2.0
    assert policy.calculate_delay(2) == 4.0
    assert policy.calculate_delay(3) == 8.0

def test_calculate_delay_linear():
    """Linear: delay = base * (attempt + 1) (no jitter)."""
    policy = RetryPolicy(backoff_strategy=BackoffStrategy.LINEAR, backoff_base_seconds=1.0, jitter=False)
    assert policy.calculate_delay(0) == 1.0
    assert policy.calculate_delay(1) == 2.0
    assert policy.calculate_delay(4) == 5.0

def test_calculate_delay_capped():
    """Delay is capped at backoff_max_seconds."""
    policy = RetryPolicy(backoff_base_seconds=1.0, backoff_max_seconds=10.0, jitter=False)
    assert policy.calculate_delay(20) == 10.0

def test_calculate_delay_negative_attempt_raises():
    """Negative attempt raises ValueError."""
    policy = RetryPolicy(jitter=False)
    with pytest.raises(ValueError):
        policy.calculate_delay(-1)

def test_calculate_delay_jitter_bounds():
    """With jitter, delay is within [base*0.75, base*1.25] for attempt 0."""
    policy = RetryPolicy(backoff_base_seconds=4.0, jitter=True)
    delays = [policy.calculate_delay(0) for _ in range(100)]
    assert all(0.0 <= d <= 5.0 for d in delays)

async def test_execute_with_retry_succeeds_first():
    """No retry when first attempt succeeds."""
    rm = RetryManager()
    call_count = 0
    async def fn():
        nonlocal call_count; call_count += 1; return {"ok": True}
    result = await rm.execute_with_retry("t1", RetryPolicy(), fn)
    assert call_count == 1

async def test_execute_with_retry_succeeds_third():
    """Retries twice, succeeds on third attempt."""
    rm = RetryManager()
    call_count = 0
    async def fn():
        nonlocal call_count; call_count += 1
        if call_count < 3: raise RuntimeError("fail")
        return {"ok": True}
    result = await rm.execute_with_retry(
        "t1", RetryPolicy(max_attempts=3, backoff_base_seconds=0.1, jitter=False), fn
    )
    assert call_count == 3

async def test_execute_with_retry_exhausted():
    """All attempts fail, raises last exception."""
    rm = RetryManager()
    async def fn(): raise RuntimeError("always fail")
    with pytest.raises(RuntimeError, match="always fail"):
        await rm.execute_with_retry(
            "t1", RetryPolicy(max_attempts=2, backoff_base_seconds=0.1, jitter=False), fn
        )

async def test_execute_with_retry_calls_on_retry():
    """on_retry callback is called before each retry."""
    rm = RetryManager()
    retry_calls = []
    async def on_retry(tid, attempt, exc):
        retry_calls.append((tid, attempt))
    async def fn():
        if len(retry_calls) < 2: raise RuntimeError("fail")
        return {"ok": True}
    await rm.execute_with_retry(
        "t1", RetryPolicy(max_attempts=3, backoff_base_seconds=0.1, jitter=False), fn, on_retry
    )
    assert len(retry_calls) == 2
```

### Unit Tests: `tests/durability/test_circuit_breaker.py`

```python
def test_initial_state_closed():
    """New circuit breaker starts CLOSED."""

def test_can_execute_when_closed():
    """CLOSED state allows execution."""

def test_opens_after_threshold():
    """Circuit opens after failure_threshold consecutive failures."""
    cb = CircuitBreaker("exec1", CircuitBreakerConfig(failure_threshold=3))
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

def test_blocks_when_open():
    """OPEN state blocks execution."""
    cb = CircuitBreaker("exec1", CircuitBreakerConfig(failure_threshold=1))
    cb.record_failure()
    assert not cb.can_execute()

def test_half_open_after_timeout():
    """OPEN transitions to HALF_OPEN after reset_timeout_seconds."""
    cb = CircuitBreaker("exec1", CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=0.1))
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    import time; time.sleep(0.15)
    assert cb.state == CircuitState.HALF_OPEN

def test_half_open_allows_limited_attempts():
    """HALF_OPEN allows half_open_max_attempts executions."""
    cb = CircuitBreaker("exec1", CircuitBreakerConfig(
        failure_threshold=1, reset_timeout_seconds=0.1, half_open_max_attempts=2
    ))
    cb.record_failure()
    import time; time.sleep(0.15)
    assert cb.can_execute()  # 1st attempt
    assert cb.can_execute()  # 2nd attempt
    assert not cb.can_execute()  # 3rd blocked

def test_half_open_success_closes():
    """Success in HALF_OPEN transitions to CLOSED."""
    cb = CircuitBreaker("exec1", CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=0.1))
    cb.record_failure()
    import time; time.sleep(0.15)
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.can_execute()

def test_half_open_failure_reopens():
    """Failure in HALF_OPEN transitions back to OPEN."""
    cb = CircuitBreaker("exec1", CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=0.1))
    cb.record_failure()
    import time; time.sleep(0.15)
    assert cb.state == CircuitState.HALF_OPEN
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

def test_success_resets_failure_count():
    """Success after some failures resets the count."""
    cb = CircuitBreaker("exec1", CircuitBreakerConfig(failure_threshold=3))
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED  # Count reset, only 2 consecutive failures

def test_force_reset():
    """reset() forces CLOSED state regardless of current state."""
    cb = CircuitBreaker("exec1", CircuitBreakerConfig(failure_threshold=1))
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    cb.reset()
    assert cb.state == CircuitState.CLOSED

def test_registry_get_creates():
    """Registry creates new breaker on first get."""
    registry = CircuitBreakerRegistry()
    cb = registry.get("exec1")
    assert cb.state == CircuitState.CLOSED

def test_registry_get_returns_same():
    """Registry returns same instance for same executor_id."""
    registry = CircuitBreakerRegistry()
    cb1 = registry.get("exec1")
    cb2 = registry.get("exec1")
    assert cb1 is cb2

def test_config_failure_threshold_boundary():
    """failure_threshold=0 raises, 1 valid, 1000 valid, 1001 raises."""

def test_config_reset_timeout_boundary():
    """reset_timeout=0.9 raises, 1.0 valid, 86400.0 valid, 86400.1 raises."""
```

### Integration Tests: `tests/durability/test_integration.py`

```python
async def test_checkpoint_resume_end_to_end(sync_db_session):
    """Retry on_retry callback saves checkpoint; next execution resumes from it.
    Steps:
    1. Create a task in the DB.
    2. Execute with retry (2 attempts); execute_fn always fails.
    3. on_retry callback (mirroring TaskManager._on_retry) saves progress as checkpoint.
    4. After retries are exhausted, verify checkpoint was persisted.
    5. Load checkpoint and inject into inputs (what TaskManager does on re-run).
    6. Verify resumed data is correct; delete checkpoint after simulated success.
    """

async def test_retry_with_circuit_breaker():
    """Repeated task-level failures open the circuit breaker, blocking further runs.
    Steps:
    1. Create CircuitBreakerRegistry with failure_threshold=3.
    2. Simulate 3 task executions: each exhausts its retry policy and records one failure.
    3. After 3 recordings, circuit is OPEN.
    4. can_execute() returns False, blocking the next attempt.
    """

async def test_checkpoint_persists_across_sessions(tmp_path):
    """Checkpoint data saved in session A is readable by a fresh session B.
    Steps:
    1. Create a SQLite database file and session A.
    2. Save checkpoint with CheckpointManager(session_A), then close session A.
    3. Open an independent session B on the same file.
    4. Load checkpoint with CheckpointManager(session_B). Verify data matches.
    """
```

---

## Execution Wiring

Durability lives in `TaskManager`, but execution runs through the singleton
`TaskExecutor`, which builds a `TaskManager` per execution. The components are
wired via the global `ConfigRegistry`: `app.py` registers the process-singleton
`CircuitBreakerRegistry` and calls `set_durability_enabled(True)`; per execution,
`TaskExecutor._build_execution_components(session)` builds the session-bound
`CheckpointManager(session)` + `RetryManager` on the live execution session and
passes them to the `TaskManager`. This makes durability apply on every execution
path (module/REST/MCP/A2A/scheduler), not just a dependency-injected instance.
See the matching section in [Cost Governance](cost-governance.md#execution-wiring).

---

## Acceptance Criteria

1. A task that fails mid-execution can be retried and resumes from its last checkpoint.
2. Retry backoff follows the configured strategy: fixed produces constant delay, exponential doubles each time, linear increases linearly.
3. Circuit breaker prevents execution when consecutive failures exceed the threshold and allows test execution after the reset timeout.
4. Checkpoint data persists across process restarts (verified via session close/reopen test).
5. Database migration 004 applies cleanly on both SQLite and PostgreSQL.
6. Tasks that do not support checkpointing still benefit from retry logic (they restart from the beginning).
7. The `supports_checkpoint()`, `get_checkpoint()`, and `resume_from_checkpoint()` methods are optional -- existing executors are unaffected.
