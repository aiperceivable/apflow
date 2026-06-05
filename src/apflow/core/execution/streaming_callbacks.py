"""
Streaming callbacks for SSE (Server-Sent Events) updates during task execution
"""

import asyncio
from typing import Dict, Any, Optional
from apflow.logger import get_logger

logger = get_logger(__name__)


class StreamingCallbacks:
    """Handles streaming updates for task execution"""

    def __init__(self, root_task_id: Optional[str] = None):
        """
        Initialize StreamingCallbacks

        Args:
            root_task_id: Optional root task ID for streaming
        """
        self.logger = logger
        self.root_task_id = root_task_id
        self.event_queue = None
        self.context = None
        # Strong references to in-flight fire-and-forget progress tasks. The event
        # loop only holds weak references to bare tasks, so without this a progress
        # update can be garbage-collected mid-flight and silently dropped.
        self._pending_tasks: set[asyncio.Task] = set()

    def set_streaming_context(self, event_queue, context):
        """
        Set streaming context for SSE updates

        Args:
            event_queue: Event queue (can be EventQueueBridge or similar)
            context: Request context
        """
        self.event_queue = event_queue
        self.context = context

    def send_update(self, update_type: str, task_id: str, status: str, **kwargs):
        """
        Send streaming update with flexible parameters

        Args:
            update_type: Type of update (task_start, task_completed, etc.)
            task_id: Task ID
            status: Task status
            **kwargs: Additional update data
        """
        progress_data = {"type": update_type, "task_id": task_id, "status": status, **kwargs}
        self._queue_progress_update(progress_data)

    def task_start(self, task_id: str, **kwargs):
        """Send task start update"""
        self.send_update("task_start", task_id=task_id, status="in_progress", **kwargs)

    def task_completed(self, task_id: str, result: Any = None, **kwargs):
        """Send task completion update"""
        self.send_update(
            "task_completed",
            task_id=task_id,
            status="completed",
            result=result,
            final=True,
            **kwargs,
        )

    def task_failed(self, task_id: str, error: str, **kwargs):
        """Send task failure update"""
        self.send_update(
            "task_failed", task_id=task_id, status="failed", error=error, final=True, **kwargs
        )

    def progress(self, task_id: str, progress: float, message: str = "", **kwargs):
        """Send progress update"""
        self.send_update(
            "progress",
            task_id=task_id,
            status="in_progress",
            progress=progress,
            message=message,
            **kwargs,
        )

    def final(
        self, task_id: str, status: str, result: Any = None, error: Optional[str] = None, **kwargs
    ):
        """Send final completion/failure update"""
        self.send_update(
            "final",
            task_id=task_id,
            status=status,
            result=result,
            error=error,
            final=True,
            **kwargs,
        )

    def _queue_progress_update(self, progress_data: Dict[str, Any]) -> None:
        """Queue progress update for synchronous callbacks"""
        try:
            from datetime import datetime

            if "timestamp" not in progress_data:
                progress_data["timestamp"] = datetime.utcnow().isoformat()

            # Check if we're already in an event loop
            try:
                asyncio.get_running_loop()
                # We're in an event loop, create a task and retain a strong reference
                # until it completes so it cannot be garbage-collected mid-flight.
                task = asyncio.create_task(self._send_progress_update(progress_data))
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)
            except RuntimeError:
                # No event loop running, use asyncio.run()
                asyncio.run(self._send_progress_update(progress_data))
        except Exception as e:
            logger.error(f"Failed to queue progress update: {str(e)}")

    async def _send_progress_update(self, progress_data: Dict[str, Any]) -> None:
        """
        Send progress update through stream manager or event queue

        Args:
            progress_data: Progress update data
        """
        try:
            # Use root task ID for stream manager if available
            self.root_task_id if self.root_task_id else progress_data.get("task_id")

            # If event_queue is set (via bridge or direct), use it
            if self.event_queue:
                # Check if it's an EventQueueBridge or similar
                if hasattr(self.event_queue, "put"):
                    await self.event_queue.put(progress_data)
                else:
                    # Direct event queue
                    logger.warning("Direct event queue handling not yet implemented")
        except Exception as e:
            logger.error(f"Failed to send progress update: {str(e)}")
