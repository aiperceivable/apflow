"""
Base Scheduler Interface

Defines the abstract interface for all scheduler implementations.
Both internal scheduler and external gateway adapters implement this interface.
"""

from abc import ABC, abstractmethod
from enum import StrEnum, auto
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime


class SchedulerState(StrEnum):
    """Scheduler runtime state"""

    stopped = auto()  # Scheduler is not running
    starting = auto()  # Scheduler is starting up
    running = auto()  # Scheduler is running and processing tasks
    stopping = auto()  # Scheduler is shutting down
    paused = auto()  # Scheduler is paused (not processing but can resume)
    error = auto()  # Scheduler encountered an error


class SchedulerConfig:
    """
    Configuration for scheduler behavior.

    Attributes:
        poll_interval: Seconds between checking for due tasks (default: 60)
        max_concurrent_tasks: Maximum tasks to execute concurrently (default: 10)
        task_timeout: Default timeout for task execution in seconds (default: 3600)
        retry_on_failure: Whether to retry failed tasks (default: False)
        max_retries: Maximum retry attempts (default: 3)
        user_id: Optional user ID filter for multi-user scenarios
    """

    def __init__(
        self,
        poll_interval: int = 60,
        max_concurrent_tasks: int = 10,
        task_timeout: int = 3600,
        retry_on_failure: bool = False,
        max_retries: int = 3,
        user_id: Optional[str] = None,
    ):
        self.poll_interval = poll_interval
        self.max_concurrent_tasks = max_concurrent_tasks
        self.task_timeout = task_timeout
        self.retry_on_failure = retry_on_failure
        self.max_retries = max_retries
        self.user_id = user_id

    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary"""
        return {
            "poll_interval": self.poll_interval,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "task_timeout": self.task_timeout,
            "retry_on_failure": self.retry_on_failure,
            "max_retries": self.max_retries,
            "user_id": self.user_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SchedulerConfig":
        """Create config from dictionary"""
        return cls(**{k: v for k, v in data.items() if k in cls.__init__.__code__.co_varnames})


class SchedulerStats:
    """
    Runtime statistics for scheduler monitoring.

    Attributes:
        state: Current scheduler state
        started_at: When the scheduler started
        tasks_executed: Total tasks executed since start
        tasks_succeeded: Tasks that completed successfully
        tasks_failed: Tasks that failed
        last_poll_at: Last time we checked for due tasks
        next_poll_at: Next scheduled poll time
        active_tasks: Currently executing task count
    """

    def __init__(self):
        self.state: SchedulerState = SchedulerState.stopped
        self.started_at: Optional[datetime] = None
        self.tasks_executed: int = 0
        self.tasks_succeeded: int = 0
        self.tasks_failed: int = 0
        self.last_poll_at: Optional[datetime] = None
        self.next_poll_at: Optional[datetime] = None
        self.active_tasks: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert stats to dictionary"""
        return {
            "state": self.state.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "tasks_executed": self.tasks_executed,
            "tasks_succeeded": self.tasks_succeeded,
            "tasks_failed": self.tasks_failed,
            "last_poll_at": self.last_poll_at.isoformat() if self.last_poll_at else None,
            "next_poll_at": self.next_poll_at.isoformat() if self.next_poll_at else None,
            "active_tasks": self.active_tasks,
        }


class BaseScheduler(ABC):
    """
    Abstract base class for all scheduler implementations.

    Implementations:
    - InternalScheduler: built-in in-process poll loop (single-node)
    - External schedulers push-trigger via the schedule.trigger module or the
      POST /webhook/trigger/{task_id} endpoint; clusters use ``apflow worker``

    Lifecycle:
        scheduler = InternalScheduler(config)
        await scheduler.start()     # Start processing
        await scheduler.pause()     # Pause processing
        await scheduler.resume()    # Resume processing
        await scheduler.stop()      # Stop and cleanup
    """

    def __init__(self, config: Optional[SchedulerConfig] = None):
        """
        Initialize scheduler with configuration.

        Args:
            config: Scheduler configuration. Uses defaults if not provided.
        """
        self.config = config or SchedulerConfig()
        self.stats = SchedulerStats()
        self._task_callbacks: List[Callable] = []

    @abstractmethod
    async def start(self) -> None:
        """
        Start the scheduler.

        Begins polling for due tasks and executing them.
        Raises RuntimeError if already running.
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """
        Stop the scheduler gracefully.

        Waits for active tasks to complete before stopping.
        """
        pass

    @abstractmethod
    async def pause(self) -> None:
        """
        Pause the scheduler.

        Stops polling for new tasks but allows active tasks to complete.
        """
        pass

    @abstractmethod
    async def resume(self) -> None:
        """
        Resume a paused scheduler.

        Resumes polling for due tasks.
        """
        pass

    @abstractmethod
    async def trigger(self, task_id: str) -> bool:
        """
        Manually trigger a specific task.

        Executes the task immediately regardless of schedule.

        Args:
            task_id: The task ID to trigger

        Returns:
            True if task was triggered successfully, False otherwise
        """
        pass

    @abstractmethod
    async def get_status(self) -> SchedulerStats:
        """
        Get current scheduler status and statistics.

        Returns:
            SchedulerStats with current state and metrics
        """
        pass

    def on_task_complete(self, callback: Callable[[str, bool, Any], None]) -> None:
        """
        Register a callback for task completion events.

        Args:
            callback: Function called with (task_id, success, result)
        """
        self._task_callbacks.append(callback)

    def _notify_task_complete(self, task_id: str, success: bool, result: Any) -> None:
        """Notify all registered callbacks of task completion"""
        for callback in self._task_callbacks:
            try:
                callback(task_id, success, result)
            except Exception:
                pass  # Don't let callback errors affect scheduler
