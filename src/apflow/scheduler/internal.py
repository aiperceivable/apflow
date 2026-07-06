"""
Internal Scheduler Implementation

Built-in scheduler for apflow that polls for due tasks and fires them
in-process against the database. Uses asyncio for lightweight scheduling
without external dependencies.

Clone-per-fire model: each fire clones the scheduled definition into a fresh
pending run instance (origin_type=scheduled_run) and executes that instance;
the definition itself never executes, it only advances its schedule. The set of
run instances is the history (see TaskRepository.list_scheduled_runs). This is
also the distributed-native model — a future worker bridge would lease the
run instances instead of executing them in-process.

Consistency model (single-node): one poll loop runs in one process, so there is
no cross-process contention. Duplicate fires are prevented by the in-process
``_active_task_ids`` set (a task already firing is skipped). Run it inside the
server process (``serve --scheduler``) so it shares the single SQLite writer.
For multi-node clusters use ``apflow worker`` instead, whose distributed runtime
leases tasks atomically via PostgreSQL — that is the supported path for
distributed coordination.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set

from apflow.scheduler.base import (
    BaseScheduler,
    SchedulerConfig,
    SchedulerState,
    SchedulerStats,
)
from apflow.logger import get_logger

logger = get_logger(__name__)


def scheduled_dispatch_key(definition: Any) -> Optional[str]:
    """Deterministic idempotency key for one scheduled occurrence.

    Derived from the definition id and its due slot time (``next_run_at``), so
    duplicate dispatches of the SAME slot — e.g. during a leader handoff where two
    nodes briefly both dispatch — produce run instances carrying the SAME key. The
    business/executor layer can then dedupe (unique constraint / optimistic lock)
    and stay effectively-once. Returns None when there is no scheduled slot (an
    ad-hoc fire has nothing to dedupe on, so each run should be distinct).
    """
    next_run_at = getattr(definition, "next_run_at", None)
    if next_run_at is None:
        return None
    digest = hashlib.sha256(f"{definition.id}|{next_run_at.isoformat()}".encode()).hexdigest()
    return f"sched-{digest}"


class InternalScheduler(BaseScheduler):
    """
    Built-in scheduler for apflow.

    Features:
    - Polls the database for due tasks at configurable intervals
    - Executes tasks in-process concurrently up to max_concurrent_tasks
    - Handles task completion and next_run calculation
    - Supports pause/resume without losing state
    - Multi-user support via user_id filter

    Usage:
        config = SchedulerConfig(
            poll_interval=60,          # Check every 60 seconds
            max_concurrent_tasks=5,    # Max 5 concurrent tasks
            user_id="user123"          # Optional: only process this user's tasks
        )
        scheduler = InternalScheduler(config)
        await scheduler.start()

    For CLI usage:
        apflow scheduler --poll-interval 60 --max-concurrent 5
    """

    def __init__(
        self,
        config: Optional[SchedulerConfig] = None,
        verbose: bool = False,
        dispatch_only: bool = False,
    ):
        super().__init__(config)
        # Dispatch-only (distributed) mode: each fire instantiates a pending run
        # instance and advances the definition's schedule, but does NOT execute
        # the run in-process — distributed workers lease and execute it. Used by
        # the leader node in a cluster so scheduling and execution are decoupled.
        self._dispatch_only: bool = dispatch_only
        self._poll_task: Optional[asyncio.Task[None]] = None
        # asyncio.Event/Semaphore constructors do not bind a loop on 3.10+, so
        # they are safe to create here (non-Optional); start() re-creates fresh
        # events to support restart.
        self._stop_event: asyncio.Event = asyncio.Event()
        self._pause_event: asyncio.Event = asyncio.Event()
        self._pause_event.set()  # Not paused initially
        self._active_task_ids: Set[str] = set()
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(self.config.max_concurrent_tasks)
        self._verbose: bool = verbose
        self._task_names: Dict[str, str] = {}
        self._console: Any = None
        if verbose:
            from rich.console import Console

            self._console = Console()

    async def start(self) -> None:
        """
        Start the scheduler.

        Begins polling the database for due tasks and executing them in-process.
        Raises RuntimeError if already running.
        """
        if self.stats.state in (SchedulerState.running, SchedulerState.starting):
            raise RuntimeError("Scheduler is already running")

        logger.info(
            f"Starting internal scheduler "
            f"(poll_interval={self.config.poll_interval}s, "
            f"max_concurrent={self.config.max_concurrent_tasks})"
        )

        self.stats.state = SchedulerState.starting
        self.stats.started_at = datetime.now(timezone.utc)

        # Initialize control events
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # Not paused initially

        # Semaphore for concurrent task limiting
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent_tasks)

        # Start the polling loop
        self._poll_task = asyncio.create_task(self._poll_loop())

        self.stats.state = SchedulerState.running
        logger.info("Internal scheduler started successfully")

    async def stop(self) -> None:
        """
        Stop the scheduler gracefully.

        Waits for active tasks to complete before stopping.
        """
        if self.stats.state == SchedulerState.stopped:
            logger.warning("Scheduler is already stopped")
            return

        logger.info("Stopping internal scheduler...")
        self.stats.state = SchedulerState.stopping

        # Signal the poll loop to stop
        if self._stop_event:
            self._stop_event.set()

        # Resume if paused (so the loop can check stop event)
        if self._pause_event:
            self._pause_event.set()

        # Wait for the poll task to finish
        if self._poll_task:
            try:
                await asyncio.wait_for(self._poll_task, timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning("Poll task did not finish in time, cancelling")
                self._poll_task.cancel()
                try:
                    await self._poll_task
                except asyncio.CancelledError:
                    pass

        # Wait for active tasks to complete (with timeout)
        if self._active_task_ids:
            logger.info(f"Waiting for {len(self._active_task_ids)} active tasks to complete...")
            # Give active tasks some time to finish
            wait_start = datetime.now(timezone.utc)
            while self._active_task_ids:
                if (datetime.now(timezone.utc) - wait_start).total_seconds() > 60:
                    logger.warning(f"Timeout waiting for active tasks: {self._active_task_ids}")
                    break
                await asyncio.sleep(1)

        self.stats.state = SchedulerState.stopped
        logger.info("Internal scheduler stopped")

    async def pause(self) -> None:
        """
        Pause the scheduler.

        Stops polling for new tasks but allows active tasks to complete.
        """
        if self.stats.state != SchedulerState.running:
            raise RuntimeError("Can only pause a running scheduler")

        logger.info("Pausing scheduler...")
        self._pause_event.clear()
        self.stats.state = SchedulerState.paused

    async def resume(self) -> None:
        """
        Resume a paused scheduler.

        Resumes polling for due tasks.
        """
        if self.stats.state != SchedulerState.paused:
            raise RuntimeError("Can only resume a paused scheduler")

        logger.info("Resuming scheduler...")
        self._pause_event.set()
        self.stats.state = SchedulerState.running

    async def trigger(self, task_id: str) -> bool:
        """
        Manually trigger a specific task.

        Executes the task immediately regardless of schedule.

        Args:
            task_id: The task ID to trigger

        Returns:
            True if task was triggered successfully, False otherwise
        """
        logger.info(f"Manually triggering task: {task_id}")

        if task_id in self._active_task_ids:
            logger.warning(f"Task {task_id} is already executing")
            return False

        # Add to active set BEFORE creating task to prevent duplicate triggers
        self._active_task_ids.add(task_id)
        try:
            asyncio.create_task(self._execute_task(task_id))
            return True
        except Exception as e:
            # Rollback if task creation fails
            self._active_task_ids.discard(task_id)
            logger.error(f"Failed to trigger task {task_id}: {e}")
            return False

    async def get_status(self) -> SchedulerStats:
        """
        Get current scheduler status and statistics.

        Returns:
            SchedulerStats with current state and metrics
        """
        self.stats.active_tasks = len(self._active_task_ids)
        return self.stats

    def _print_task_result(
        self, task_id: str, task_name: str, status: str, error: Optional[str] = None
    ) -> None:
        """Print task execution result to console in verbose mode."""
        if not self._console:
            return
        status_colors = {
            "completed": "green",
            "failed": "red",
            "pending": "yellow",
            "in_progress": "cyan",
        }
        color = status_colors.get(status, "red")
        status_display = f"[{color}]{status}[/{color}]"
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"  [blue][{now}][/blue]  {status_display}  {task_id}  {task_name}"
        if error and status != "completed":
            line += f"\n           [red]Error: {error}[/red]"
        self._console.print(line)

    async def _poll_loop(self) -> None:
        """
        Main polling loop that checks for due tasks.

        Runs until stop_event is set.
        """
        logger.debug("Poll loop started")

        while not self._stop_event.is_set():
            try:
                # Wait if paused
                await self._pause_event.wait()

                # Check if we should stop
                if self._stop_event.is_set():
                    break

                # Record poll time
                self.stats.last_poll_at = datetime.now(timezone.utc)
                self.stats.next_poll_at = self.stats.last_poll_at + timedelta(
                    seconds=self.config.poll_interval
                )

                # Get due tasks
                due_tasks = await self._get_due_tasks()

                if due_tasks:
                    logger.debug(f"Found {len(due_tasks)} due tasks")
                    if self._console:
                        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
                        self._console.print(
                            f"[blue][{now}][/blue] Found {len(due_tasks)} due task(s)"
                        )

                    # Execute due tasks (respecting concurrency limit).
                    # _get_due_tasks returns to_dict()'d rows, so each task is a dict.
                    for task in due_tasks:
                        if self._stop_event.is_set():
                            break

                        task_id = task.get("id", "")
                        if self._verbose:
                            task_name = task.get("name", "")
                            self._task_names[task_id] = task_name
                        if task_id not in self._active_task_ids:
                            # Add to active set BEFORE creating task to prevent
                            # duplicate scheduling during semaphore wait
                            self._active_task_ids.add(task_id)
                            try:
                                asyncio.create_task(self._execute_task(task_id))
                            except Exception as e:
                                # Rollback if task creation fails
                                self._active_task_ids.discard(task_id)
                                logger.error(f"Failed to schedule task {task_id}: {e}")

                # Wait for next poll interval (or until stopped)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.config.poll_interval
                    )
                    # If we get here, stop_event was set
                    break
                except asyncio.TimeoutError:
                    # Normal timeout, continue polling
                    pass

            except Exception as e:
                logger.error(f"Error in poll loop: {e}", exc_info=True)
                self.stats.state = SchedulerState.error
                # Wait a bit before retrying
                await asyncio.sleep(5)
                if self.stats.state == SchedulerState.error:
                    self.stats.state = SchedulerState.running

        logger.debug("Poll loop ended")

    async def _get_due_tasks(self) -> List[Dict[str, Any]]:
        """Get tasks that are due for execution (direct database access).

        Returns:
            List of task dictionaries that are due
        """
        try:
            from apflow.core.storage import create_pooled_session
            from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

            async with create_pooled_session() as db_session:
                task_repository = TaskRepository(db_session)

                tasks = await task_repository.get_due_scheduled_tasks(
                    before=datetime.now(timezone.utc),
                    user_id=self.config.user_id,
                    limit=self.config.max_concurrent_tasks * 2,  # Fetch more than we can execute
                )

                return [task.to_dict() for task in tasks]

        except Exception as e:
            logger.error(f"Failed to get due tasks: {e}", exc_info=True)
            return []

    async def _execute_task(self, task_id: str) -> None:
        """
        Execute a single scheduled task in-process against the database.

        Args:
            task_id: The task ID to execute

        Note:
            task_id must already be in _active_task_ids before calling this method.
            The caller (poll_loop or trigger) is responsible for adding it.
        """
        try:
            # Acquire semaphore to limit concurrency
            async with self._semaphore:
                self.stats.active_tasks = len(self._active_task_ids)
                await self._execute_task_via_db(task_id)

        finally:
            # Always remove from active tasks, even if semaphore wait was cancelled.
            # This prevents deadlock when task is added to _active_task_ids but
            # never gets to execute due to cancellation or other errors.
            self._active_task_ids.discard(task_id)
            self.stats.active_tasks = len(self._active_task_ids)

    async def _execute_task_via_db(self, task_id: str) -> None:
        """Fire a scheduled task via direct database access (clone-per-fire).

        Each fire clones the scheduled definition into a fresh pending run
        instance (origin_type=scheduled_run) and executes that instance. The
        definition itself never executes — it only advances its schedule. Run
        history is the set of instances (see TaskRepository.list_scheduled_runs).
        """
        success = False
        result = None
        error = None
        run_id: Optional[str] = None
        count_this_run = True
        task_name = self._task_names.pop(task_id, "")

        try:
            logger.debug(f"Firing scheduled task via DB: {task_id}")

            from apflow.core.storage import create_pooled_session
            from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
            from apflow.core.execution.task_creator import TaskCreator
            from apflow.core.execution.task_executor import TaskExecutor

            # Spawn a fresh run instance from the definition. The definition is
            # never executed in place — the clone is what runs, giving isolated
            # per-fire history and matching the distributed one-task-id-per-run
            # model.
            async with create_pooled_session() as db_session:
                task_repository = TaskRepository(db_session)
                definition = await task_repository.get_task_by_id(task_id)
                if not definition:
                    logger.debug(f"Scheduled task {task_id} not found")
                    return
                task_name = task_name or getattr(definition, "name", "")
                # Stamp a deterministic occurrence key so a duplicate dispatch of
                # this same slot (e.g. a leader handoff) is recognizable and can be
                # deduped by the business layer (effectively-once).
                run_tree = await TaskCreator(db_session).instantiate_scheduled_run(
                    definition, idempotency_key=scheduled_dispatch_key(definition)
                )
                if run_tree is None:
                    # This slot's occurrence was already dispatched (duplicate
                    # rejected by the unique key). Nothing runs here. The finally
                    # block still advances next_run_at (guaranteeing progress even
                    # if this was a persistent save failure rather than a genuine
                    # duplicate) but does NOT count the run — the winning node
                    # already counted it.
                    logger.info(f"Scheduled task {task_id} occurrence already dispatched; skipping")
                    count_this_run = False
                    return
                run_id = str(run_tree.task.id)

            if self._dispatch_only:
                # Distributed mode: leave the run instance pending for a worker to
                # lease and execute. The dispatch (fire) itself succeeded; the run's
                # own outcome is recorded by whichever worker executes it. The finally
                # block still advances the definition's schedule.
                success = True
                self.stats.tasks_executed += 1
                self.stats.tasks_succeeded += 1
                logger.info(
                    f"Dispatched run {run_id} for schedule {task_id} (pending worker execution)"
                )
                self._print_task_result(task_id, task_name, "dispatched", error=None)
                return

            # Execute the run instance in-process (single-node mode).
            task_executor = TaskExecutor()

            async with create_pooled_session() as db_session:
                task_repository = TaskRepository(db_session)

                run = await task_repository.get_task_by_id(run_id)
                if not run:
                    logger.error(f"Run instance {run_id} not found for schedule {task_id}")
                    return

                # Load the run tree from DB — unified with the tree execution model.
                run_tree = await task_repository.get_task_tree_for_api(run)
                logger.debug(
                    f"Executing run {run_id} for schedule {task_id}: "
                    f"{len(run_tree.children)} children"
                )

                await task_executor.execute_task_tree(
                    task_tree=run_tree,
                    root_task_id=run_id,
                    use_streaming=False,
                    db_session=db_session,
                )

                # Refresh the run instance to get its result.
                run = await task_repository.get_task_by_id(run_id)
                if run:
                    success = run.status == "completed"
                    result = run.result
                    if run.error:
                        error = run.error

                    # Print children results in verbose mode
                    if self._console and getattr(run, "has_children", False):
                        children = await task_repository.get_child_tasks_by_parent_id(run_id)
                        for child in children:
                            self._print_task_result(
                                child.id,
                                getattr(child, "name", ""),
                                child.status,
                                error=child.error,
                            )

            self.stats.tasks_executed += 1
            if success:
                self.stats.tasks_succeeded += 1
                logger.info(f"Scheduled task {task_id} run {run_id} completed successfully")
            elif error:
                self.stats.tasks_failed += 1
                logger.warning(f"Scheduled task {task_id} run {run_id} failed: {error}")
            else:
                self.stats.tasks_failed += 1
                logger.debug(f"Scheduled task {task_id} run {run_id} not completed")

            self._print_task_result(
                task_id, task_name, "completed" if success else "failed", error=error
            )

        except Exception as e:
            success = False
            error = str(e)
            self.stats.tasks_executed += 1
            self.stats.tasks_failed += 1
            logger.error(f"Error firing scheduled task {task_id}: {e}", exc_info=True)
            self._print_task_result(task_id, task_name, "failed", error=error)

        finally:
            # Advance the definition's schedule (next run time, run_count).
            try:
                from apflow.core.storage import create_pooled_session
                from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

                async with create_pooled_session() as db_session:
                    task_repository = TaskRepository(db_session)
                    await task_repository.complete_scheduled_run(
                        task_id=task_id,
                        success=success,
                        error=error,
                        calculate_next_run=True,
                        count_run=count_this_run,
                    )
            except Exception as e:
                logger.error(f"Failed to complete scheduled run for {task_id}: {e}")

            # Notify callbacks
            self._notify_task_complete(task_id, success, result)


async def run_scheduler(config: Optional[SchedulerConfig] = None, verbose: bool = False) -> None:
    """
    Run the internal scheduler (blocking).

    This is a convenience function for running the scheduler as a standalone process.
    Use Ctrl+C to stop.

    Args:
        config: Optional scheduler configuration
        verbose: Show task execution results in console

    Usage:
        import asyncio
        from apflow.scheduler import run_scheduler, SchedulerConfig

        config = SchedulerConfig(poll_interval=30)
        asyncio.run(run_scheduler(config))
    """
    scheduler = InternalScheduler(config, verbose=verbose)

    # Handle shutdown signals
    import signal

    def signal_handler() -> None:
        logger.info("Received shutdown signal")
        asyncio.create_task(scheduler.stop())

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await scheduler.start()
        # Keep running until stopped
        while scheduler.stats.state in (SchedulerState.running, SchedulerState.paused):
            await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"Scheduler error: {e}", exc_info=True)
    finally:
        if scheduler.stats.state != SchedulerState.stopped:
            await scheduler.stop()


def make_scheduler_lifespan(config: Optional[SchedulerConfig] = None) -> Any:
    """Build a Starlette lifespan that runs the scheduler in the server process.

    This is the single-node consistency path: the poll loop shares the server's
    event loop (and SQLite single writer), so there is no second process
    contending for the database. Used by ``apflow rest --scheduler`` and
    ``serve --all --scheduler``; clusters use ``apflow worker`` instead.
    """
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        scheduler = InternalScheduler(config)
        await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()

    return lifespan
