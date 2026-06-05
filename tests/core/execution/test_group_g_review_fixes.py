"""Regression tests for review group G (registry/scanner/streaming cleanup)."""

from __future__ import annotations

import asyncio

import pytest

from apflow.core.execution.streaming_callbacks import StreamingCallbacks


class TestStreamingTaskRetention:
    """Fire-and-forget progress tasks must be strongly referenced until done."""

    @pytest.mark.asyncio
    async def test_progress_task_retained_then_discarded(self) -> None:
        cb = StreamingCallbacks()
        # No event_queue → _send_progress_update is a no-op, but the task still runs.
        cb._queue_progress_update({"task_id": "t", "progress": 0.5})

        # Synchronously retained right after create_task (cannot be GC'd mid-flight).
        assert len(cb._pending_tasks) == 1

        await asyncio.sleep(0.01)

        # Discarded via done-callback once it completes.
        assert len(cb._pending_tasks) == 0


class TestExecutorRegistryRemoved:
    """The dead parallel ExecutorRegistry module is gone (issue 32)."""

    def test_module_import_fails(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            __import__("apflow.core.execution.executor_registry")

    def test_attribute_removed_from_package(self) -> None:
        import apflow.core.execution as execution_pkg

        with pytest.raises(AttributeError):
            _ = execution_pkg.ExecutorRegistry
