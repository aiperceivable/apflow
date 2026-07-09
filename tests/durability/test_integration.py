"""Durability wiring integration tests (F-003).

Cover the cross-component behaviors that unit tests cannot: that CheckpointManager
persists data across sessions, that RetryManager's on_retry callback wires correctly
to CheckpointManager, and that CircuitBreaker blocks execution after repeated failures.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apflow.core.storage.sqlalchemy.models import Base, TaskModel
from apflow.durability.checkpoint import CheckpointManager
from apflow.durability.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitState,
)
from apflow.durability.retry import RetryManager, RetryPolicy


@pytest.mark.asyncio
async def test_checkpoint_resume_end_to_end(sync_db_session):
    """A failed task saves a checkpoint via the on_retry callback; the checkpoint
    can be loaded and injected into the next execution as TaskManager does.

    Steps:
    1. Create a task in the DB.
    2. Execute with retry (2 attempts); execute_fn always fails.
    3. on_retry callback (mirroring TaskManager._on_retry) saves a checkpoint.
    4. After retries are exhausted, verify the checkpoint was persisted.
    5. Load the checkpoint and inject it into inputs (what TaskManager does on
       the next execution) — verify the resumed data is correct.
    6. Delete the checkpoint (what TaskManager does after success) — verify cleanup.
    """
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

    repo = TaskRepository(sync_db_session)
    task = await repo.create_task(name="resumable_task", max_attempts=2)

    checkpoint_mgr = CheckpointManager(sync_db_session)
    retry_mgr = RetryManager()

    progress_state = {"steps": ["step_1", "step_2", "step_3"]}

    async def on_retry(tid: str, _attempt: int, _exc: Exception) -> None:
        # Mirror what TaskManager._on_retry does: save executor progress as a checkpoint.
        await checkpoint_mgr.save_checkpoint(tid, dict(progress_state))

    async def execute_fn() -> dict:
        raise RuntimeError("synthetic failure before step_4")

    policy = RetryPolicy(max_attempts=2, backoff_base_seconds=0.1, jitter=False)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        await retry_mgr.execute_with_retry(task.id, policy, execute_fn, on_retry)

    # Checkpoint must have been saved by the on_retry callback after attempt 0.
    checkpoint_data = await checkpoint_mgr.load_checkpoint(task.id)
    assert checkpoint_data is not None
    assert checkpoint_data["steps"] == ["step_1", "step_2", "step_3"]

    # Simulate TaskManager's resume path: inject checkpoint into inputs for next run.
    final_inputs: dict = {}
    final_inputs["_checkpoint"] = checkpoint_data
    resumed_steps = list(final_inputs["_checkpoint"]["steps"])
    resumed_steps.append("step_4")  # executor resumes from checkpoint and completes
    assert resumed_steps == ["step_1", "step_2", "step_3", "step_4"]

    # Successful completion should remove the checkpoint (what TaskManager does).
    deleted = await checkpoint_mgr.delete_checkpoints(task.id)
    assert deleted == 1
    assert await checkpoint_mgr.load_checkpoint(task.id) is None


@pytest.mark.asyncio
async def test_retry_with_circuit_breaker():
    """Repeated task-level failures open the circuit breaker, blocking further execution.

    Steps:
    1. Create a CircuitBreakerRegistry with failure_threshold=3.
    2. Simulate 3 separate task executions, each exhausting its retry policy.
    3. After each execution failure, record a failure on the circuit breaker
       (mirroring what TaskManager._execute_single_task does).
    4. After 3 recordings, verify the circuit is OPEN.
    5. Verify can_execute() returns False, blocking the 4th attempt.
    """
    registry = CircuitBreakerRegistry(default_config=CircuitBreakerConfig(failure_threshold=3))
    retry_mgr = RetryManager()

    policy = RetryPolicy(max_attempts=2, backoff_base_seconds=0.1, jitter=False)

    async def always_fail() -> dict:
        raise RuntimeError("always fails")

    executor_name = "flaky_executor"

    # 3 task-level executions: each exhausts retries then records one failure.
    for i in range(3):
        cb = registry.get(executor_name)
        assert cb.can_execute(), f"Circuit should still be CLOSED before run {i + 1}"
        with pytest.raises(RuntimeError, match="always fails"):
            await retry_mgr.execute_with_retry(f"task_{i}", policy, always_fail)
        cb.record_failure()  # What TaskManager does after retry exhaustion

    cb = registry.get(executor_name)
    assert cb.state == CircuitState.OPEN
    assert not cb.can_execute()  # 4th execution is blocked by the open circuit


@pytest.mark.asyncio
async def test_checkpoint_persists_across_sessions(tmp_path):
    """Checkpoint data saved in session A is readable by a fresh session B on the same database.

    Steps:
    1. Create a SQLite database file and session A.
    2. Save a checkpoint with session A, then close it.
    3. Open an independent session B on the same file.
    4. Load the checkpoint with session B and verify the data matches.
    """
    db_file = str(tmp_path / "durability_persist_test.db")
    db_url = f"sqlite:///{db_file}"

    # --- Session A: create task and save checkpoint ---
    engine_a = create_engine(db_url)
    Base.metadata.create_all(engine_a)
    session_a = sessionmaker(bind=engine_a)()

    task = TaskModel.create({"name": "persist_test_task"})
    session_a.add(task)
    session_a.commit()
    task_id = str(task.id)

    original_data = {"progress": 0.75, "steps_done": ["a", "b", "c"], "result_so_far": 42}
    mgr_a = CheckpointManager(session_a)
    cp_id = await mgr_a.save_checkpoint(task_id, original_data)
    assert cp_id is not None

    session_a.close()
    engine_a.dispose()

    # --- Session B: load checkpoint from the same file ---
    engine_b = create_engine(db_url)
    session_b = sessionmaker(bind=engine_b)()

    mgr_b = CheckpointManager(session_b)
    loaded = await mgr_b.load_checkpoint(task_id)

    session_b.close()
    engine_b.dispose()

    assert loaded is not None
    assert loaded == original_data
