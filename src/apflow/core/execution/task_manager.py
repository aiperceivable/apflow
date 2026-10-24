"""
Task management service for orchestrating and executing tasks
"""

from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, Any, Optional, List, Union, cast
import asyncio
from copy import deepcopy
from decimal import Decimal
from inspect import iscoroutinefunction
from apflow.core.storage.sqlalchemy.models import TaskModel

if TYPE_CHECKING:
    from apflow.durability.checkpoint import CheckpointManager
    from apflow.durability.retry import RetryManager
    from apflow.durability.circuit_breaker import CircuitBreakerRegistry
    from apflow.governance.budget import BudgetManager
    from apflow.governance.policy import PolicyEngine
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.core.storage.context import set_hook_context, clear_hook_context
from apflow.core.execution.streaming_callbacks import StreamingCallbacks
from apflow.core.extensions import get_registry, ExtensionCategory, Extension
from apflow.core.types import (
    TaskTreeNode,
    TaskPreHook,
    TaskPostHook,
)
from apflow.core.config import (
    get_pre_hooks,
    get_post_hooks,
    get_task_model_class,
    get_task_tree_hooks,
)
from apflow.core.validator.dependency_validator import (
    are_dependencies_satisfied,
)
from apflow.core.execution.errors import (
    BusinessError,
    ConfigurationError,
    ExecutorError,
    ValidationError,
)
from apflow.core.execution.context import current_invocation_id
from apflow.core.execution.result_protocol import ExecutorResult, normalize_executor_result
from apflow.core.execution.error_info import ErrorInfo, execution_error_info
from apflow.logger import get_logger

logger = get_logger(__name__)


def _preflight_failure_info(
    task: TaskModel, code: str, phase: str, category: str = "permanent"
) -> ErrorInfo:
    """Describe an admission failure whose executor was never invoked."""
    info = execution_error_info(
        ValidationError(code), task_id=str(task.id), attempt_id=task.attempt_id or 0, phase=phase
    )
    info["code"], info["category"] = code, category
    return info


class TaskManager:
    """Schedule pending run tasks and record each physical invocation independently.

    Managed retries create a new run. Completed nodes supply prior results while
    their organizational descendants remain traversable.
    """

    def __init__(
        self,
        db: Union[Session, AsyncSession],
        root_task_id: Optional[str] = None,
        pre_hooks: Optional[List[TaskPreHook]] = None,
        post_hooks: Optional[List[TaskPostHook]] = None,
        executor_instances: Optional[Dict[str, Any]] = None,
        cancellation_events: Optional[Dict[str, asyncio.Event]] = None,
        use_demo: bool = False,
        checkpoint_manager: Optional["CheckpointManager"] = None,
        retry_manager: Optional["RetryManager"] = None,
        circuit_breaker_registry: Optional["CircuitBreakerRegistry"] = None,
        budget_manager: Optional["BudgetManager"] = None,
        policy_engine: Optional["PolicyEngine"] = None,
    ):
        """
        Initialize TaskManager

        Args:
            db: Database session (sync or async)
            root_task_id: Optional root task ID for streaming
            pre_hooks: Optional list of pre-execution hook functions
                Each hook receives (task: TaskModel)
                Hooks can access and modify task.inputs directly
                Hooks can be sync or async functions
                Example:
                    async def my_pre_hook(task):
                        # Custom validation or transformation
                        if task.inputs and task.inputs.get("url"):
                            task.inputs["url"] = task.inputs["url"].strip()
                    task_manager = TaskManager(db, pre_hooks=[my_pre_hook])
            post_hooks: Optional list of post-execution hook functions
                Each hook receives (task: TaskModel, inputs: Dict[str, Any], result: Any)
                Hooks can be sync or async functions
                Example:
                    async def my_post_hook(task, inputs, result):
                        # Custom result processing or logging
                        # Raw result may contain credentials/PII — keep at DEBUG.
                        logger.debug(f"Task {task.id} completed with result: {result}")
                    task_manager = TaskManager(db, post_hooks=[my_post_hook])
            executor_instances: Optional shared dictionary for storing executor instances (task_id -> executor)
                Used for cancellation support. If provided, executors created during execution are stored here
                so that cancel_task() can access them. Typically passed from TaskExecutor.
            use_demo: If True, executors return demo data instead of executing (default: False)
                This is an execution option, not a task input. It's passed as a parameter to TaskManager
                and used by TaskManager._execute_task_with_schemas() to determine whether to return demo data.
        """
        self.db = db
        self.is_async = isinstance(db, AsyncSession)
        self.root_task_id = root_task_id
        # Get task_model_class from config registry (supports custom TaskModel via decorators)
        task_model_class = get_task_model_class()
        self.task_repository = TaskRepository(db, task_model_class=task_model_class)
        self.streaming_callbacks = StreamingCallbacks(root_task_id=self.root_task_id)
        self.stream = False
        self.streaming_final = False
        # Use provided hooks or fall back to config registry
        # This allows hooks to be registered globally via decorators
        self.pre_hooks = pre_hooks if pre_hooks is not None else get_pre_hooks()
        self.post_hooks = post_hooks if post_hooks is not None else get_post_hooks()
        # Store executor instances for cancellation (task_id -> executor)
        # Use shared executor_instances dict from TaskExecutor if provided, otherwise create new one
        # This allows cancel_task() to access executors created during execution
        self._executor_instances: Dict[str, Any] = (
            executor_instances if executor_instances is not None else {}
        )
        # Lock to protect concurrent access to _executor_instances from parallel tasks
        self._executor_lock = asyncio.Lock()
        self._cancellation_events = cancellation_events if cancellation_events is not None else {}
        # Track tasks that should be re-executed (even if they are completed or failed)
        # This allows re-executing failed tasks and ensures dependencies are also re-executed
        self._tasks_to_reexecute: set[str] = set()
        self._execution_attempt_ids: Dict[str, int] = {}
        self._attempt_numbers: Dict[str, int] = {}
        self._attempt_records: Dict[str, str] = {}
        self._executable_task_ids: Optional[set[str]] = None
        # Demo mode flag - if True, executors return demo data instead of executing
        self.use_demo = use_demo
        # Durability (F-003): checkpoint, retry, circuit breaker
        self._checkpoint_manager = checkpoint_manager
        self._retry_manager = retry_manager
        self._circuit_breaker_registry = circuit_breaker_registry
        # Governance (F-004): budget, policy
        self._budget_manager = budget_manager
        self._policy_engine = policy_engine

    async def cancel_task(
        self, task_id: str, error_message: Optional[str] = None
    ) -> Dict[str, Any]:
        """Persist cancellation first and report whether executor stop is confirmed."""
        task = await self.task_repository.request_cancellation(task_id)
        if task is None:
            return {"status": "failed", "message": f"Task {task_id} not found"}
        if task.status in {"completed", "failed", "cancelled"}:
            return {
                "status": "cancelled" if task.status == "cancelled" else "failed",
                "current_status": task.status,
                "cancel_requested": task.cancel_requested_at is not None,
                "stop_confirmed": task.status == "cancelled",
                "message": f"Task {task_id} is already {task.status}",
            }
        event = self._cancellation_events.setdefault(task_id, asyncio.Event())
        event.set()
        stopped = task.status == "pending"
        attempt_id = task.attempt_id
        response = await self._notify_cancellation(task_id)
        stopped = stopped or response.get("status") == "cancelled"
        message = error_message or response.get("message") or "Cancellation requested"
        fields: Dict[str, Any] = {"error": message}
        if stopped:
            fields.update(status="cancelled", completed_at=datetime.now(timezone.utc))
            if "result" in response or "partial_result" in response:
                fields["result"] = deepcopy(response.get("result", response.get("partial_result")))
            updated = await self.task_repository.transition_task(
                task_id,
                expected_statuses=["pending", "in_progress"],
                expected_attempt_id=attempt_id,
                allow_cancel_requested=True,
                **fields,
            )
            stopped = updated is not None
            if not stopped:
                fields.pop("result", None)
        usage = await self._record_cancellation_usage(
            task_id, attempt_id, response.get("token_usage")
        )
        result = {
            "status": "cancelled" if stopped else "cancelling",
            "cancel_requested": True,
            "stop_confirmed": stopped,
            "message": message,
        }
        if "result" in fields:
            result["result"] = fields["result"]
        if usage:
            result["token_usage"] = usage
        return result

    async def _notify_cancellation(self, task_id: str) -> Dict[str, Any]:
        """Notify a local executor without treating a missing handle as confirmed stop."""
        async with self._executor_lock:
            executor = self._executor_instances.get(task_id)
        if executor is None or not hasattr(executor, "cancel"):
            return {}
        try:
            return await executor.cancel() or {}
        except Exception as exc:
            logger.warning("Executor cancellation failed for task %s: %s", task_id, exc)
            return {}

    async def _record_cancellation_usage(
        self, task_id: str, generation: int, usage: Any
    ) -> Optional[Dict[str, int]]:
        if not usage:
            return None
        try:
            normalized = normalize_executor_result(
                {"result": None, "token_usage": usage}, "envelope"
            )
            normalized_usage = normalized.get("token_usage")
            if normalized_usage:
                from apflow.durability.attempts import AttemptRecorder

                await AttemptRecorder(self.db).record_partial(task_id, generation, normalized_usage)
            return normalized_usage
        except ValidationError as exc:
            logger.warning("Task %s: invalid cancellation usage: %s", task_id, exc)
            return None

    async def _confirm_requested_cancellation(
        self, task: TaskModel, expected_attempt_id: Optional[int] = None
    ) -> bool:
        """A finished local attempt confirms stop without accepting its late output."""
        if expected_attempt_id is not None and task.attempt_id != expected_attempt_id:
            return True
        if task.status == "cancelled":
            return True
        if task.cancel_requested_at is None:
            return False
        await self.task_repository.transition_task(
            str(task.id),
            expected_statuses=["pending", "in_progress"],
            expected_attempt_id=task.attempt_id,
            allow_cancel_requested=True,
            status="cancelled",
            completed_at=datetime.now(timezone.utc),
        )
        return True

    def _get_execution_tree(self, task_tree: TaskTreeNode) -> TaskTreeNode:
        """Build a summary of selected tasks while retaining the distribution tree."""
        if self._executable_task_ids is None:
            return task_tree
        selected_tasks = [
            node.task for node in task_tree if str(node.task.id) in self._executable_task_ids
        ]
        requested_task = next(task for task in selected_tasks if str(task.id) == self.root_task_id)
        execution_tree = TaskTreeNode(requested_task)
        for task in selected_tasks:
            if task.id != requested_task.id:
                execution_tree.add_child(TaskTreeNode(task))
        return execution_tree

    async def distribute_task_tree(
        self, task_tree: TaskTreeNode, use_callback: bool = True
    ) -> TaskTreeNode:
        """
        Distribute task tree directly with proper multi-level priority execution

        Args:
            task_tree: Root task tree node
            use_callback: Whether to use callbacks

        Returns:
            Task tree node with execution results
        """
        logger.info(f"Distributing task tree with root task: {task_tree.task.id}")

        # Set hook context for DB access in hooks
        set_hook_context(self.task_repository)

        try:
            execution_tree = self._get_execution_tree(task_tree)
            root_task = execution_tree.task

            # Call on_tree_created hook
            await self._call_task_tree_hooks("on_tree_created", root_task, execution_tree)

            # Call on_tree_started hook
            await self._call_task_tree_hooks("on_tree_started", root_task)

            try:
                # Execute task tree
                await self._execute_task_tree_recursive(task_tree, use_callback)

                # Check final status
                final_status = execution_tree.calculate_status()

                # Call on_tree_completed hook
                if final_status == "completed":
                    await self._call_task_tree_hooks("on_tree_completed", root_task, "completed")
                else:
                    # Tree finished but not all tasks completed (some failed)
                    await self._call_task_tree_hooks(
                        "on_tree_failed", root_task, f"Tree finished with status: {final_status}"
                    )

                return task_tree
            except Exception as e:
                # Call on_tree_failed hook
                if root_task is not None:
                    await self._call_task_tree_hooks("on_tree_failed", root_task, str(e))
                raise
        finally:
            # Clear hook context
            clear_hook_context()

    async def distribute_task_tree_with_streaming(
        self, task_tree: TaskTreeNode, use_callback: bool = True
    ) -> None:
        """
        Distribute task tree with real-time streaming for progress updates

        Args:
            task_tree: Root task tree node
            use_callback: Whether to use callbacks
        """
        logger.info(f"Distributing task tree with streaming, root task: {task_tree.task.id}")

        # Set hook context for DB access in hooks
        set_hook_context(self.task_repository)

        try:
            # Enable streaming mode and set root task ID
            self.stream = True
            self.streaming_final = False
            execution_tree = self._get_execution_tree(task_tree)
            root_task = execution_tree.task
            self.root_task_id = root_task.id

            # Call on_tree_created hook
            await self._call_task_tree_hooks("on_tree_created", root_task, execution_tree)

            # Call on_tree_started hook
            await self._call_task_tree_hooks("on_tree_started", root_task)

            root_task_id = root_task.id
            try:
                # Send initial status
                self.streaming_callbacks.progress(root_task.id, 0.0, "Task tree execution started")

                # Save task IDs before execution to avoid accessing them after session rollback
                root_task_id = root_task.id
                task_tree_root_id = root_task.id

                # Execute task tree with progress streaming
                await self._execute_task_tree_recursive(task_tree, use_callback)

                # Check final status
                final_progress = execution_tree.calculate_progress()
                final_status = execution_tree.calculate_status()

                # Ensure progress is a float
                if isinstance(final_progress, Decimal):
                    final_progress = float(final_progress)

                finished_root = await self.task_repository.get_task_by_id(str(task_tree_root_id))
                business_result = finished_root.result if finished_root is not None else None

                # Send final status if all tasks are completed
                if final_status == "completed":
                    self.streaming_callbacks.final(
                        task_tree_root_id,
                        final_status,
                        result=business_result,
                        progress=final_progress,
                    )
                    # Call on_tree_completed hook
                    await self._call_task_tree_hooks("on_tree_completed", root_task, "completed")
                else:
                    # Send final progress update
                    if final_status == "failed":
                        self.streaming_callbacks.final(
                            task_tree_root_id,
                            "failed",
                            result=business_result,
                            error=f"Task tree execution {final_status}",
                            progress=final_progress,
                        )
                    else:
                        self.streaming_callbacks.progress(
                            task_tree_root_id, final_progress, f"Task tree execution {final_status}"
                        )
                    # Call on_tree_failed hook
                    await self._call_task_tree_hooks(
                        "on_tree_failed", root_task, f"Tree finished with status: {final_status}"
                    )

            except Exception as e:
                logger.error(f"Error in distribute_task_tree_with_streaming: {str(e)}")
                # Use saved task ID to avoid accessing task after session rollback
                try:
                    task_tree_root_id = root_task.id if root_task is not None else root_task_id
                except Exception:
                    task_tree_root_id = root_task_id
                self.streaming_callbacks.task_failed(task_tree_root_id, str(e))
                # Call on_tree_failed hook
                if root_task is not None:
                    await self._call_task_tree_hooks("on_tree_failed", root_task, str(e))
                raise
        finally:
            # Clear hook context
            clear_hook_context()

    async def _execute_task_tree_recursive(
        self, node: TaskTreeNode, use_callback: bool = True
    ) -> None:
        """
        Execute task tree recursively with proper dependency checking

        Managed runs schedule pending nodes, traverse completed structural
        ancestors, and retain prior failed or completed execution history.

        Process:
        1. Check streaming final status and task re-execution eligibility
        2. Group children by priority level
        3. Execute each priority group (ready tasks in parallel, waiting tasks deferred)
        4. Execute current task if dependencies satisfied

        Args:
            node: Task tree node to execute
            use_callback: Whether to use callbacks
        """
        node_task_id = self._get_safe_task_id(node.task)

        try:
            # Check if streaming has been marked as final
            if self.streaming_final:
                logger.info(
                    f"Streaming marked as final, stopping task tree execution for {node_task_id}"
                )
                return

            # Structural ancestors remain traversable without running their executors.
            task_id = str(node.task.id)
            if self._executable_task_ids is not None and task_id not in self._executable_task_ids:
                for child in sorted(node.children, key=lambda item: item.task.priority or 999):
                    await self._execute_task_tree_recursive(child, use_callback)
                return

            # Check if node task should be processed
            if node.task.status in ["completed", "in_progress"]:
                if task_id not in self._tasks_to_reexecute:
                    logger.info(
                        f"Task {node_task_id} already {node.task.status}, skipping distribution"
                    )
                    if node.task.run_id and node.task.status == "completed":
                        groups = self._group_children_by_priority_for_execution(node)
                        for priority in sorted(groups):
                            await self._execute_priority_group(groups[priority], priority)
                    return
                else:
                    logger.info(
                        f"Task {node_task_id} is {node.task.status} but marked for re-execution, will re-execute"
                    )
            elif node.task.status == "failed":
                logger.info(f"Task {node_task_id} is failed, will re-execute")

            # Group children by priority for execution
            priority_groups = self._group_children_by_priority_for_execution(node)

            # If no children, check if current task should execute based on dependencies
            if not priority_groups:
                deps_satisfied = await self._are_dependencies_satisfied(node.task)
                if deps_satisfied and node.task.status != "completed":
                    logger.debug(
                        f"All dependencies for task {node_task_id} are satisfied, executing task"
                    )
                    await self._execute_single_task(node.task, use_callback)
                else:
                    logger.debug(
                        f"No children to execute for task {node_task_id}, dependencies not satisfied or already completed"
                    )
                return

            # Execute each priority group
            sorted_priorities = sorted(priority_groups.keys())
            logger.debug(
                f"Executing {len(node.children)} children for task {node_task_id} in {len(sorted_priorities)} priority groups"
            )

            for priority in sorted_priorities:
                await self._execute_priority_group(priority_groups[priority], priority)

            # After processing all children, check if current task should execute
            deps_satisfied = await self._are_dependencies_satisfied(node.task)
            if deps_satisfied and node.task.status != "completed":
                logger.debug(
                    f"All dependencies for task {node_task_id} are satisfied, executing task"
                )
                await self._execute_single_task(node.task, use_callback)

        except Exception as e:
            task_id_str = str(node_task_id) if node_task_id else "unknown"
            # Log business errors without stack trace, unexpected errors with stack trace
            if isinstance(e, BusinessError):
                logger.error(
                    f"Business error in _execute_task_tree_recursive for task {task_id_str}: {str(e)}"
                )
            else:
                logger.error(
                    f"Error in _execute_task_tree_recursive for task {task_id_str}: {str(e)}",
                    exc_info=True,
                )

            # Update task status if possible
            try:
                if node_task_id:
                    await self.task_repository.transition_task(
                        task_id=node_task_id,
                        expected_statuses=["pending", "in_progress"],
                        expected_attempt_id=node.task.attempt_id,
                        status="failed",
                        error=str(e),
                        error_info=execution_error_info(
                            e,
                            task_id=node_task_id,
                            attempt_id=node.task.attempt_id,
                            phase="orchestration",
                        ),
                        completed_at=datetime.now(timezone.utc),
                    )
            except Exception as db_error:
                logger.error(f"Error updating task status in database: {str(db_error)}")
            raise

    def _group_children_by_priority_for_execution(
        self, node: TaskTreeNode
    ) -> Dict[int, List[TaskTreeNode]]:
        """
        Group children by priority level, respecting task re-execution rules

        Includes:
        - Pending/failed tasks
        - Completed tasks marked for re-execution
        - All their descendants recursively

        Args:
            node: Parent task tree node

        Returns:
            Dictionary mapping priority level to list of task nodes
        """
        priority_groups = {}

        for child_node in node.children:
            priority = child_node.task.priority or 999
            if priority not in priority_groups:
                priority_groups[priority] = []

            child_id = str(child_node.task.id)

            # Check if child should be included based on status and re-execution rules
            should_include = False

            if child_node.task.status not in ["completed", "failed"]:
                # Pending tasks always included
                should_include = True
            elif child_node.task.status == "failed":
                # Failed tasks always included
                should_include = True
            elif child_node.task.status == "completed" and child_id in self._tasks_to_reexecute:
                # Completed tasks only included if marked for re-execution
                should_include = True

            if should_include:
                priority_groups[priority].append(child_node)
                # Recursively add descendants
                self._add_children_to_priority_groups(child_node, priority_groups)
            elif child_node.task.run_id and child_node.task.status == "completed":
                self._add_children_to_priority_groups(child_node, priority_groups)

        return priority_groups

    async def _execute_priority_group(self, tasks: List[TaskTreeNode], priority: int) -> None:
        """
        Execute a group of tasks with the same priority level

        Separates tasks into ready (dependencies satisfied) and waiting (dependencies pending)
        Ready tasks execute in parallel. Waiting tasks are left for later triggering.

        Args:
            tasks: List of task nodes with same priority
            priority: Priority level being processed
        """
        logger.debug(f"Processing {len(tasks)} tasks with priority {priority}")

        # Separate ready and waiting tasks
        ready_tasks = []
        waiting_tasks = []

        for task_node in tasks:
            deps_satisfied = await are_dependencies_satisfied(
                task_node.task, self.task_repository, self._tasks_to_reexecute
            )
            if deps_satisfied:
                ready_tasks.append(task_node)
            else:
                waiting_tasks.append(task_node)

        # Execute ready tasks
        if ready_tasks:
            if len(ready_tasks) == 1:
                # Single task - execute directly
                child_node = ready_tasks[0]
                await self._execute_single_task(child_node.task, use_callback=True)
                await self._execute_task_tree_recursive(child_node, use_callback=True)
            else:
                # Multiple tasks - execute in parallel
                logger.debug(
                    f"Executing {len(ready_tasks)} ready tasks in parallel with priority {priority}"
                )

                async def execute_child_and_descendants(child_node):
                    await self._execute_single_task(child_node.task, use_callback=True)
                    await self._execute_task_tree_recursive(child_node, use_callback=True)

                parallel_tasks = [
                    execute_child_and_descendants(child_node) for child_node in ready_tasks
                ]

                # return_exceptions=True so one branch's failure does not cancel the
                # other in-flight sibling branches (which would leave them stuck
                # in_progress). After all branches settle, preserve the original
                # fail-the-tree contract by re-raising the first real failure.
                results = await asyncio.gather(*parallel_tasks, return_exceptions=True)
                failures = [r for r in results if isinstance(r, Exception)]
                for err in failures:
                    logger.error(
                        f"Parallel branch failed at priority {priority}: {err}",
                        exc_info=err,
                    )
                logger.debug(f"Completed parallel execution of {len(ready_tasks)} ready tasks")
                if failures:
                    raise failures[0]

        # Log waiting tasks for later execution
        if waiting_tasks:
            logger.debug(f"Leaving {len(waiting_tasks)} tasks waiting for dependencies")

    def _add_children_to_priority_groups(
        self, node: TaskTreeNode, priority_groups: Dict[int, List[TaskTreeNode]]
    ):
        """Recursively add all children to priority groups.

        Note: this flattens the whole subtree into priority groups, and
        _execute_priority_group ALSO recurses via _execute_task_tree_recursive, so a
        descendant can be dispatched at multiple levels. This is intentional and safe
        — duplicate dispatch collapses to a single execution via the atomic
        compare-and-swap claim in try_claim_for_execution (a second claim of an
        already-claimed task is rejected). It is a known efficiency tradeoff; do not
        "fix" it by dropping one traversal without re-validating cross-level priority
        ordering and fan-in against the full integration suite.
        """
        for child_node in node.children:
            priority = child_node.task.priority or 999
            if priority not in priority_groups:
                priority_groups[priority] = []
            child_id = str(child_node.task.id)
            # Include pending tasks, failed tasks, and completed tasks marked for re-execution
            if child_node.task.status not in ["completed", "failed"]:
                priority_groups[priority].append(child_node)
                self._add_children_to_priority_groups(child_node, priority_groups)
            elif child_node.task.status == "failed":
                # Failed tasks should be re-executed
                priority_groups[priority].append(child_node)
                self._add_children_to_priority_groups(child_node, priority_groups)
            elif child_node.task.status == "completed" and child_id in self._tasks_to_reexecute:
                # Completed tasks marked for re-execution should be re-executed
                priority_groups[priority].append(child_node)
                self._add_children_to_priority_groups(child_node, priority_groups)
            elif child_node.task.run_id and child_node.task.status == "completed":
                self._add_children_to_priority_groups(child_node, priority_groups)

    def _get_safe_task_id(self, task: TaskModel) -> Optional[str]:
        """
        Safely extract task ID avoiding SQLAlchemy lazy loading after session rollback

        Uses multiple strategies to get the ID:
        1. Direct __dict__ access (no SQLAlchemy trigger)
        2. SQLAlchemy inspect for persistent/detached objects
        3. Direct attribute access (fallback, may trigger lazy loading)

        Args:
            task: Task object to extract ID from

        Returns:
            Task ID string, or None if ID cannot be extracted
        """
        if not task:
            return None

        try:
            # Strategy 1: Try direct __dict__ access to avoid SQLAlchemy lazy loading
            task_id = task.__dict__.get("id") if hasattr(task, "__dict__") else None
            if task_id:
                return str(task_id)

            # Strategy 2: Use SQLAlchemy inspect for persistent/detached objects
            from sqlalchemy import inspect as sa_inspect

            insp = sa_inspect(task)
            if insp.persistent or insp.detached:
                identity = insp.identity
                if identity:
                    return str(identity[0])

            # Strategy 3: Direct attribute access (last resort, may trigger lazy loading)
            return str(task.id)
        except Exception:
            # If all methods fail, return None
            return None

    async def _check_task_execution_preconditions(self, task: TaskModel, task_id: str) -> bool:
        """
        Check if task can be executed (status, streaming final, cancellation, in-progress)

        Args:
            task: Task to check
            task_id: Task ID string

        Returns:
            True if task should proceed with execution, False if it should be skipped
        """
        # Check if streaming has been marked as final
        if self.streaming_final:
            logger.info(f"Streaming marked as final, stopping single task execution for {task_id}")
            return False

        # Skip completed/cancelled tasks unless marked for re-execution
        if task.origin_type in {"archive", "link"}:
            return False
        if task.status in ["completed", "cancelled"]:
            if task_id not in self._tasks_to_reexecute:
                logger.info(f"Task {task_id} already {task.status}, skipping execution")
                return False
            else:
                logger.info(
                    f"Task {task_id} is {task.status} but marked for re-execution, will re-execute"
                )
                return True

        # Skip in_progress tasks (already being executed)
        if task.status == "in_progress":
            logger.info(f"Task {task_id} already in_progress, skipping execution")
            return False

        # Failed tasks should be re-executed if marked, otherwise skip
        if task.status == "failed":
            if task_id in self._tasks_to_reexecute:
                logger.info(
                    f"Task {task_id} is failed and marked for re-execution, will re-execute"
                )
                return True
            else:
                logger.info(
                    f"Task {task_id} is failed but not marked for re-execution, skipping execution"
                )
                return False

        # Pending tasks proceed normally
        return True

    async def _check_cancellation_and_refresh_task(
        self, task_id: str, stage: str = "pre-execution"
    ) -> Optional[TaskModel]:
        """
        Check if task was cancelled and refresh task from database

        Args:
            task_id: Task ID to check
            stage: Current stage of execution (for logging)

        Returns:
            Refreshed task if not cancelled, None if task was cancelled
        """
        # Refresh session state before query to ensure we see latest database state
        # This prevents blocking in sync sessions when there are uncommitted transactions
        if not self.is_async:
            self.db.expire_all()
        task = await self.task_repository.get_task_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        if task.status == "cancelled" or task.cancel_requested_at is not None:
            await self._confirm_requested_cancellation(task)
            logger.info(f"Task {task_id} was cancelled {stage}, stopping execution")
            return None

        return task

    async def _resolve_and_update_task_inputs(
        self, task: TaskModel, task_id: str
    ) -> Dict[str, Any]:
        """
        Resolve task dependencies and update inputs in database if changed

        Args:
            task: Task to resolve dependencies for
            task_id: Task ID string

        Returns:
            Resolved task inputs
        """
        # Resolve dependencies first (merge dependency results into inputs)
        try:
            resolved_inputs = await self.resolve_task_dependencies(task)
        except Exception as exc:
            if not getattr(exc, "error_info", None):
                exc.error_info = _preflight_failure_info(  # type: ignore[attr-defined]
                    task, "DEPENDENCY_RESOLUTION_FAILED", "dependency", "dependency"
                )
            raise

        # Check cancellation before proceeding
        refreshed_task = await self._check_cancellation_and_refresh_task(
            task_id, "during dependency resolution"
        )
        if not refreshed_task:
            return {}
        task = refreshed_task

        # Update inputs if they changed
        if resolved_inputs != (task.inputs or {}):
            logger.debug(f"Dependency resolution modified inputs for task {task_id}")
            await self.task_repository.update_task(task_id, inputs=resolved_inputs)
            # Refresh task object
            refreshed_task = await self._check_cancellation_and_refresh_task(
                task_id, "after input update"
            )
            if not refreshed_task:
                return {}
            task = refreshed_task

        return resolved_inputs

    async def _execute_and_apply_pre_hooks(self, task: TaskModel, task_id: str) -> Dict[str, Any]:
        """
        Execute pre-hooks and apply any input modifications to database

        Args:
            task: Task to execute pre-hooks for
            task_id: Task ID string

        Returns:
            Final inputs after pre-hook modifications
        """
        import copy

        # Store inputs before pre-hooks to detect changes
        inputs_before_pre_hooks = copy.deepcopy(task.inputs) if task.inputs else {}

        # Execute pre-hooks
        await self._execute_pre_hooks(task)

        # Check if pre-hooks modified inputs
        inputs_after_pre_hooks = task.inputs or {}

        if inputs_after_pre_hooks != inputs_before_pre_hooks:
            logger.info(
                f"Pre-hooks modified inputs for task {task_id}: "
                f"before_keys={list(inputs_before_pre_hooks.keys())}, "
                f"after_keys={list(inputs_after_pre_hooks.keys())}"
            )
            # Save modified inputs
            inputs_to_save = copy.deepcopy(inputs_after_pre_hooks) if inputs_after_pre_hooks else {}
            await self.task_repository.update_task(task_id, inputs=inputs_to_save)

            # Refresh task and check cancellation
            refreshed_task = await self._check_cancellation_and_refresh_task(
                task_id, "after pre-hook input update"
            )
            if not refreshed_task:
                return {}
            task = refreshed_task

            logger.info(f"Pre-hooks modified inputs for task {task_id}, updated in database")
        else:
            logger.debug(f"Pre-hooks did not modify inputs for task {task_id}")

        return task.inputs or {}

    async def _update_token_usage(self, task_id: str, usage: Dict[str, int]) -> None:
        """Persist execution metadata regardless of budget-policy configuration."""
        from apflow.governance.budget import BudgetManager

        manager = self._budget_manager or BudgetManager(self.task_repository)
        await manager.update_usage(task_id, usage)

    async def _handle_task_execution_result(
        self,
        task: TaskModel,
        task_id: str,
        task_result: ExecutorResult,
        *,
        usage_recorded: bool = False,
        expected_attempt_id: Optional[int] = None,
    ) -> None:
        """
        Handle task execution result: update status, clear executor, call post-hooks

        Args:
            task: Executed task
            task_id: Task ID string
            task_result: Result from executor
        """
        attempt_id = task.attempt_id if expected_attempt_id is None else expected_attempt_id
        token_usage = task_result.get("token_usage")
        # Check if task was cancelled during execution
        # Refresh session state before query to ensure we see latest database state
        # This prevents blocking in sync sessions when there are uncommitted transactions
        if not self.is_async:
            self.db.expire_all()
        refreshed_task = await self.task_repository.get_task_by_id(task_id)
        if refreshed_task is None:
            raise ValueError(f"Task {task_id} not found after execution")
        task = refreshed_task
        if await self._confirm_requested_cancellation(task, attempt_id):
            logger.info(f"Task {task_id} was cancelled during execution, stopping")
            # Clear executor reference
            async with self._executor_lock:
                executor = self._executor_instances.pop(task_id, None)
            if executor and hasattr(executor, "clear_task_context"):
                executor.clear_task_context()
                logger.debug(f"Cleared task context for task {task_id} after cancellation")

            if self.stream:
                cancellation_callback = getattr(self.streaming_callbacks, "task_cancelled", None)
                if cancellation_callback is not None:
                    cancellation_callback(task_id)
                else:
                    self.streaming_callbacks.task_failed(task_id, "Task was cancelled")
            return

        if token_usage and not usage_recorded:
            await self._update_token_usage(task_id, token_usage)
        # Clear executor reference after successful execution
        async with self._executor_lock:
            executor = self._executor_instances.pop(task_id, None)
        if executor and hasattr(executor, "clear_task_context"):
            executor.clear_task_context()
            logger.debug(f"Cleared task context for task {task_id} after successful execution")

        # Check if the result indicates an error (e.g., executor creation failed).
        # Require a truthy error value: a successful payload that merely carries an
        # "error": None / "" field must not be misclassified as a failure.
        error_message = task_result.get("error")
        if error_message:
            # Task failed - update status to failed
            updated = await self.task_repository.transition_task(
                task_id=task_id,
                expected_statuses=(
                    ["in_progress"] if usage_recorded else ["pending", "in_progress", "completed"]
                ),
                expected_attempt_id=attempt_id,
                status="failed",
                progress=0.0,
                result=task_result["result"],
                error=error_message,
                error_info=task_result.get("error_info")
                or execution_error_info(
                    ExecutorError(error_message), task_id=task_id, attempt_id=attempt_id
                ),
                completed_at=datetime.now(timezone.utc),
            )

            if updated is None:
                return
            # Notify streaming callback
            if self.stream:
                self.streaming_callbacks.task_failed(task_id, error_message)
        else:
            # Task completed successfully
            updated = await self.task_repository.transition_task(
                task_id=task_id,
                expected_statuses=(
                    ["in_progress"] if usage_recorded else ["pending", "in_progress", "completed"]
                ),
                expected_attempt_id=attempt_id,
                status="completed",
                progress=1.0,
                result=task_result["result"],
                error=None,
                error_info=None,
                completed_at=datetime.now(timezone.utc),
            )

            if updated is None:
                return
            # --- Durability: Record success, clean up checkpoints ---
            if self._circuit_breaker_registry:
                executor_id = self._get_executor_id(task)
                if executor_id:
                    self._circuit_breaker_registry.get(executor_id).record_success()
            if self._checkpoint_manager:
                try:
                    await self._checkpoint_manager.delete_checkpoints(task_id)
                except Exception as e:
                    logger.debug(f"Task {task_id}: checkpoint cleanup: {e}")

            # Refresh and notify
            if not self.is_async:
                self.db.expire_all()
            refreshed_task = await self.task_repository.get_task_by_id(task_id)
            if refreshed_task is None:
                raise ValueError(f"Task {task_id} not found after completion update")
            task = refreshed_task

            if self.stream:
                self.streaming_callbacks.task_completed(task_id, result=task.result)

        # Trigger dependent tasks and execute post-hooks.
        # Retry on failure to prevent permanently orphaned parent tasks when
        # this is the last child to complete (no future callback will re-check).
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self.execute_after_task(task)
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    delay = 0.1 * (2**attempt)  # 0.1s, 0.2s
                    logger.warning(
                        f"execute_after_task failed for {task_id} "
                        f"(attempt {attempt + 1}/{max_retries}), retrying in {delay}s: {e}"
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        f"execute_after_task failed for {task_id} after {max_retries} attempts: {e}",
                        exc_info=True,
                    )

    async def _are_dependencies_satisfied(self, task: TaskModel) -> bool:
        """
        Check if all dependencies for a task are satisfied

        This is a wrapper around the dependency_resolver.are_dependencies_satisfied
        function that passes the necessary context from TaskManager.

        Args:
            task: Task to check dependencies for

        Returns:
            True if all dependencies are satisfied, False otherwise
        """
        try:
            return await are_dependencies_satisfied(
                task, self.task_repository, self._tasks_to_reexecute
            )
        except Exception as exc:
            if not getattr(exc, "error_info", None):
                exc.error_info = _preflight_failure_info(  # type: ignore[attr-defined]
                    task, "DEPENDENCY_CHECK_FAILED", "dependency", "dependency"
                )
            raise

    async def _fail_before_execution(self, task: TaskModel, message: str, info: ErrorInfo) -> None:
        """Persist a fenced admission failure before any provider invocation."""
        task_id = str(task.id)
        updated = await self.task_repository.transition_task(
            task_id=task_id,
            expected_statuses=["in_progress"],
            expected_attempt_id=task.attempt_id,
            status="failed",
            error=message,
            error_info=info,
            completed_at=datetime.now(timezone.utc),
        )
        if self.stream and updated is not None:
            self.streaming_callbacks.task_failed(task_id, message)

    async def _apply_budget_policy(self, task: "TaskModel", inputs: Dict[str, Any]) -> bool:
        """Apply configured thresholds before execution; return whether to proceed."""
        from apflow.governance.policy import PolicyAction

        if self._budget_manager is None:
            return True
        task_id = str(task.id)
        budget = await self._budget_manager.check_budget(task_id)
        if budget.utilization < 0:
            return True
        error = "Token budget exhausted and no cost policy configured."
        error_code = "BUDGET_EXHAUSTED"
        if self._policy_engine and task.cost_policy:
            try:
                evaluation = self._policy_engine.evaluate(
                    task.cost_policy,
                    budget.utilization,
                    current_model_index=(task.params or {}).get("_downgrade_model_index", 0),
                )
            except KeyError as exc:
                raise ConfigurationError(
                    str(exc),
                    error_info=_preflight_failure_info(task, "COST_POLICY_NOT_FOUND", "governance"),
                ) from exc
            if evaluation.action == PolicyAction.DOWNGRADE and evaluation.model_override:
                inputs["model"] = evaluation.model_override
                params = dict(task.params or {})
                params["_downgrade_model_index"] = evaluation.next_model_index
                await self.task_repository.update_task(task_id=task_id, params=params)
                logger.info(f"Task {task_id}: downgrading model to {evaluation.model_override}")
            elif evaluation.action == PolicyAction.NOTIFY:
                logger.warning(f"Task {task_id}: {evaluation.message}")
            if evaluation.action != PolicyAction.BLOCK:
                return True
            error = f"Budget policy blocked execution: {evaluation.message}"
            error_code = "BUDGET_POLICY_BLOCKED"
        elif budget.allowed:
            return True
        await self._fail_before_execution(
            task, error, _preflight_failure_info(task, error_code, "governance")
        )
        return False

    async def _execute_single_task(self, task: TaskModel, use_callback: bool = True):
        """
        Execute a single task with proper status management and error handling

        Orchestrates the complete task execution lifecycle:
        1. Pre-execution checks (status, streaming, cancellation)
        2. Status update to in_progress
        3. Dependency resolution and input preparation
        4. Pre-hook execution
        5. Task execution via executor
        6. Result handling and post-hook execution
        7. Dependent task triggering

        Args:
            task: Task to execute
            use_callback: Whether to use callbacks
        """
        task_id = self._get_safe_task_id(task)
        if task_id is None:
            logger.error("Cannot execute task without an identifier")
            return
        if self._executable_task_ids is not None and task_id not in self._executable_task_ids:
            return

        execution_attempt_id = task.attempt_id
        try:
            # Check pre-execution conditions (streaming final, basic status)
            if not await self._check_task_execution_preconditions(task, task_id):
                return

            # Refresh task and check cancellation
            refreshed_task = await self._check_cancellation_and_refresh_task(task_id)
            if not refreshed_task:
                return
            task = refreshed_task

            # Atomically claim task for execution (compare-and-swap).
            # Determines which statuses are claimable based on re-execution context.
            allowed_statuses = ["pending"]
            if task_id in self._tasks_to_reexecute:
                allowed_statuses.extend(["completed", "cancelled", "failed"])
            claimed_task = await self.task_repository.try_claim_for_execution(
                task_id, allowed_statuses=allowed_statuses
            )
            if claimed_task is None:
                logger.info(f"Task {task_id} already claimed by another caller, skipping")
                return
            task = claimed_task
            execution_attempt_id = task.attempt_id
            self._execution_attempt_ids[task_id] = execution_attempt_id

            # Emit task_start only after successful claim to avoid false events
            if self.stream:
                self.streaming_callbacks.task_start(task_id)

            logger.info(f"Task {task_id} status updated to in_progress")

            # Resolve dependencies and update inputs
            await self._resolve_and_update_task_inputs(task, task_id)

            # Final cancellation check
            refreshed_task = await self._check_cancellation_and_refresh_task(
                task_id, "before execution"
            )
            if not refreshed_task:
                return
            task = refreshed_task

            # Execute pre-hooks
            final_inputs = await self._execute_and_apply_pre_hooks(task, task_id)

            # Final cancellation check before execution
            refreshed_task = await self._check_cancellation_and_refresh_task(
                task_id, "before executor call"
            )
            if not refreshed_task:
                return
            task = refreshed_task

            logger.info(f"Task {task_id} execution - calling agent executor (name: {task.name})")

            # --- Durability: Circuit breaker pre-check ---
            if self._circuit_breaker_registry:
                executor_id = self._get_executor_id(task)
                cb = self._circuit_breaker_registry.get(executor_id) if executor_id else None
                if cb and not cb.can_execute():
                    error_msg = f"Circuit breaker OPEN for executor '{executor_id}'"
                    logger.warning(f"Task {task_id}: {error_msg}")
                    await self._fail_before_execution(
                        task,
                        error_msg,
                        _preflight_failure_info(task, "CIRCUIT_OPEN", "admission", "transient"),
                    )
                    return

            if not await self._apply_budget_policy(task, final_inputs):
                return

            # --- Durability: Load checkpoint ---
            if self._checkpoint_manager:
                checkpoint_data = await self._checkpoint_manager.load_checkpoint(task_id)
                if checkpoint_data is not None:
                    logger.info(f"Task {task_id}: resuming from checkpoint")
                    # Merge checkpoint data into inputs for executor
                    final_inputs = final_inputs or {}
                    final_inputs["_checkpoint"] = checkpoint_data

            # --- Execute task (with optional retry) ---
            if self._retry_manager and task.max_attempts and task.max_attempts > 1:
                from apflow.durability.retry import RetryPolicy, BackoffStrategy

                policy = RetryPolicy(
                    max_attempts=task.max_attempts,
                    backoff_strategy=BackoffStrategy(task.backoff_strategy or "exponential"),
                    backoff_base_seconds=float(task.backoff_base_seconds or 1.0),
                    retry_unknown_errors=False,
                )

                async def _execute_fn() -> ExecutorResult:
                    attempt_inputs = dict(final_inputs or {})
                    if self._checkpoint_manager:
                        checkpoint = await self._checkpoint_manager.load_checkpoint(task_id)
                        if checkpoint is not None:
                            attempt_inputs["_checkpoint"] = checkpoint
                    return await self._execute_attempt(task, attempt_inputs)

                async def _on_retry(tid: str, attempt: int, exc: Exception):
                    logger.warning(f"Task {tid} retry {attempt + 1}: {exc}")
                    if self._checkpoint_manager:
                        # Save checkpoint on retry if executor supports it
                        async with self._executor_lock:
                            executor = self._executor_instances.get(task_id)
                        if executor and hasattr(executor, "get_checkpoint"):
                            cp_data = executor.get_checkpoint()
                            if cp_data:
                                await self._checkpoint_manager.save_checkpoint(tid, cp_data)
                    retry_at = getattr(exc, "next_retry_at", None)
                    record_id = self._attempt_records.get(tid)
                    if record_id and isinstance(retry_at, datetime):
                        from apflow.durability.attempts import AttemptRecorder

                        await AttemptRecorder(self.db).schedule_retry(record_id, retry_at)
                    # Update attempt count
                    await self.task_repository.update_task(
                        task_id=tid,
                        attempt_count=attempt + 1,
                    )

                async def cancellation_check() -> bool:
                    return (
                        await self._check_cancellation_and_refresh_task(
                            task_id, "during retry backoff"
                        )
                        is None
                    )

                task_result = await self._retry_manager.execute_with_retry(
                    task_id,
                    policy,
                    _execute_fn,
                    _on_retry,
                    cancellation_check=cancellation_check,
                )
            else:
                task_result = await self._execute_attempt(task, final_inputs)

            # Handle execution result
            await self._handle_task_execution_result(
                task,
                task_id,
                task_result,
                usage_recorded=True,
                expected_attempt_id=execution_attempt_id,
            )

        except asyncio.CancelledError:
            current = await self.task_repository.get_task_by_id(task_id)
            if current is None or not await self._confirm_requested_cancellation(
                current, execution_attempt_id
            ):
                raise
        except Exception as e:
            # Log business errors without stack trace, unexpected errors with stack trace
            if isinstance(e, BusinessError):
                logger.error(f"Business error executing task {task_id}: {str(e)}")
            else:
                logger.error(f"Error executing task {task_id}: {str(e)}", exc_info=True)

            # --- Durability: Record failure on circuit breaker ---
            self._record_breaker_failure(task, e)

            # Retries (if any) are now exhausted — clear the executor reference
            # left in place by _execute_task_with_schemas so a retry callback
            # could still read get_checkpoint() from it between attempts.
            async with self._executor_lock:
                executor = self._executor_instances.pop(task_id, None)
            if executor and hasattr(executor, "clear_task_context"):
                executor.clear_task_context()

            # Update task status
            if task_id:
                try:
                    current = await self.task_repository.get_task_by_id(task_id)
                    if current is not None and not await self._confirm_requested_cancellation(
                        current, execution_attempt_id
                    ):
                        await self.task_repository.transition_task(
                            task_id=task_id,
                            expected_statuses=["in_progress"],
                            expected_attempt_id=execution_attempt_id,
                            status="failed",
                            error=str(e),
                            error_info=execution_error_info(
                                e, task_id=task_id, attempt_id=task.attempt_id
                            ),
                            completed_at=datetime.now(timezone.utc),
                        )
                except Exception as update_error:
                    logger.warning(f"Failed to update task status for {task_id}: {update_error}")

            # Notify streaming callback
            if self.stream and task_id:
                try:
                    self.streaming_callbacks.task_failed(task_id, str(e))
                except Exception as callback_error:
                    logger.warning(
                        f"Failed to call task_failed callback for {task_id}: {callback_error}"
                    )
        finally:
            if self._execution_attempt_ids.get(task_id) == execution_attempt_id:
                self._execution_attempt_ids.pop(task_id, None)
                self._cancellation_events.pop(task_id, None)
                async with self._executor_lock:
                    executor = self._executor_instances.pop(task_id, None)
                if executor is not None and hasattr(executor, "clear_task_context"):
                    executor.clear_task_context()

    async def _execute_pre_hooks(self, task: TaskModel) -> None:
        """
        Execute pre-execution hooks

        Args:
            task: Task to execute (hooks can access and modify task.inputs)

        Note:
            Pre-hooks are executed after dependency resolution, so task.inputs
            contains the complete resolved data including dependency results.
            Hooks can modify task.inputs directly.
        """
        if not self.pre_hooks:
            return

        logger.debug(f"Executing {len(self.pre_hooks)} pre-hooks for task {task.id}")

        for hook in self.pre_hooks:
            try:
                if iscoroutinefunction(hook):
                    await hook(task)
                else:
                    # Synchronous function - run in executor to avoid blocking
                    await asyncio.to_thread(hook, task)
            except Exception as e:
                # Log error but don't fail the task execution
                logger.warning(
                    f"Pre-hook {hook.__name__} failed for task {task.id}: {str(e)}. "
                    f"Continuing with task execution."
                )

    async def _execute_post_hooks(
        self, task: TaskModel, inputs: Dict[str, Any], result: Any
    ) -> None:
        """
        Execute post-execution hooks

        Args:
            task: Task that was executed
            inputs: Input parameters used for execution
            result: Task execution result
        """
        if not self.post_hooks:
            return

        logger.debug(f"Executing {len(self.post_hooks)} post-hooks for task {task.id}")

        for hook in self.post_hooks:
            try:
                if iscoroutinefunction(hook):
                    await hook(task, inputs, result)
                else:
                    # Synchronous function - run in executor to avoid blocking
                    await asyncio.to_thread(hook, task, inputs, result)
            except Exception as e:
                # Log error but don't fail the task execution
                logger.warning(
                    f"Post-hook {hook.__name__} failed for task {task.id}: {str(e)}. "
                    f"Task execution already completed."
                )

    async def _get_root_task(self, task: TaskModel) -> TaskModel:
        """Get root task of the task tree"""
        # Use repository method
        return await self.task_repository.get_root_task(task)

    async def _get_all_tasks_in_tree(self, root_task: TaskModel) -> List[TaskModel]:
        """
        Get all tasks in the task tree (recursive)

        Args:
            root_task: Root task of the tree

        Returns:
            List of all tasks in the tree
        """
        # Use repository method
        return await self.task_repository.get_all_tasks_in_tree(root_task)

    def _check_executor_permission(
        self,
        executor_id: str,
    ) -> bool:
        """
        Check if executor is allowed based on APFLOW_EXTENSIONS configuration

        Args:
            executor_id: Executor ID to check
            allowed_executor_ids: Set of allowed executor IDs (None means no restrictions)
            task: Task being executed
            task_type: Optional task type for error message

        Returns:
            Error dictionary if executor is not allowed, None if allowed
        """

        # Check executor permissions (if APFLOW_EXTENSIONS is set)
        # Import here to avoid circular dependency
        from apflow.core.extensions.manager import get_allowed_executor_ids

        allowed_executor_ids = get_allowed_executor_ids()

        if allowed_executor_ids is None:
            return True

        if executor_id in allowed_executor_ids:
            return True

        return False

    async def _call_task_tree_hooks(self, hook_type: str, root_task: TaskModel, *args):
        """
        Call task tree lifecycle hooks

        Args:
            hook_type: Hook type ("on_tree_created", "on_tree_started", "on_tree_completed", "on_tree_failed")
            root_task: Root task of the task tree
            *args: Additional arguments to pass to hooks
        """
        hooks = get_task_tree_hooks(hook_type)
        if not hooks:
            return

        logger.debug(
            f"Calling {len(hooks)} task tree hooks for '{hook_type}' on root task {root_task.id}"
        )

        for hook in hooks:
            try:
                if iscoroutinefunction(hook):
                    await hook(root_task, *args)
                else:
                    # Synchronous function - run in executor to avoid blocking
                    await asyncio.to_thread(hook, root_task, *args)
            except Exception as e:
                # Log error but don't fail the task tree execution
                logger.warning(
                    f"Task tree hook '{hook_type}' {hook.__name__ if hasattr(hook, '__name__') else str(hook)} "
                    f"failed for root task {root_task.id}: {str(e)}. Continuing with task tree execution."
                )

    async def execute_after_task(self, completed_task: TaskModel):
        """
        Execute after task completion - execute post-hooks and trigger dependent tasks.

        Exceptions from post-hooks are isolated (logged but swallowed) so they
        never prevent dependent task triggering.  Exceptions from the dependency
        check / trigger phase are allowed to propagate so the caller's retry
        loop in ``_handle_task_execution_result`` can re-attempt the operation.

        Args:
            completed_task: Task that just completed

        Raises:
            Exception: If the dependency check or dependent task triggering
                fails.  Post-hook errors are logged but never raised.

        Note:
            Post-hooks are executed FIRST (before triggering dependent tasks) to ensure
            immediate response for notifications, logging, etc. This allows:
            - Immediate notification of task completion
            - Fast logging and data export
            - Better user experience (no waiting for dependent tasks)

            If you need dependent task results in post-hooks, handle it in the
            dependent task's own post-hooks instead.
        """
        # Check if task is actually completed
        if completed_task.status != "completed":
            return

        # --- Phase 1: Post-hooks (errors isolated, never block Phase 2) ---
        try:
            # Refresh session state before query to ensure we see latest database state
            # This prevents blocking in sync sessions when there are uncommitted transactions
            if not self.is_async:
                self.db.expire_all()
            refreshed_task = await self.task_repository.get_task_by_id(completed_task.id)
            if refreshed_task and refreshed_task.status == "completed":
                # Get the inputs that were used for execution
                # This should include pre-hook modifications since they were saved to DB
                # Use refreshed_task.inputs which contains the latest data from database
                logger.info(
                    f"Loading task {completed_task.id} from DB for post-hook: "
                    f"inputs_type={type(refreshed_task.inputs)}, "
                    f"inputs_keys={list(refreshed_task.inputs.keys()) if refreshed_task.inputs else []}, "
                    f"inputs_value={refreshed_task.inputs}"
                )
                inputs = refreshed_task.inputs or {}
                result = refreshed_task.result

                # Ensure we're passing the actual inputs dict (not a reference that might be stale)
                # Make a copy to ensure we're passing the current state
                # If inputs is already a dict, create a shallow copy; otherwise convert to dict
                if isinstance(inputs, dict):
                    inputs = dict(inputs)
                else:
                    # Handle case where inputs might be a JSON string or other type
                    inputs = dict(inputs) if inputs else {}

                logger.info(
                    f"Post-hook inputs for task {refreshed_task.id}: "
                    f"keys={list(inputs.keys())}, has_pre_hook_marker={inputs.get('_pre_hook_executed', False)}, "
                    f"inputs_type={type(inputs)}, inputs_value={inputs}"
                )

                await self._execute_post_hooks(refreshed_task, inputs, result)
            else:
                logger.warning(
                    f"Task {completed_task.id} not found or not completed, skipping post-hooks"
                )
        except Exception as e:
            # Post-hook failures must not prevent dependent task triggering
            logger.error(
                f"Post-hook phase failed for {completed_task.id}: {str(e)}",
                exc_info=True,
            )

        # --- Phase 2: Dependency check & trigger (errors propagate for retry) ---
        logger.info(
            f"🔍 Checking for dependent tasks after completion of {completed_task.id} (name: {completed_task.name})"
        )

        # Expire cached ORM state so the tree query sees the latest
        # committed data from other coroutines (e.g. sibling tasks that
        # completed concurrently via asyncio.gather).
        if not self.is_async:
            self.db.expire_all()

        # Get all tasks in the tree
        root_task = await self._get_root_task(completed_task)
        all_tasks = await self._get_all_tasks_in_tree(root_task)

        # Find tasks that are waiting and might have their dependencies satisfied
        waiting_tasks = [
            t
            for t in all_tasks
            if t.status in ["pending", "in_progress"]
            and t.id != completed_task.id
            and (self._executable_task_ids is None or str(t.id) in self._executable_task_ids)
        ]

        # Trigger dependent tasks if any
        if waiting_tasks:
            logger.info(f"Found {len(waiting_tasks)} waiting tasks to check for dependencies")

            # Check each waiting task to see if its dependencies are now satisfied
            triggered_tasks = []
            for task in waiting_tasks:
                logger.debug(f"Checking dependencies for task {task.id} (name: {task.name})")
                deps_satisfied = await self._are_dependencies_satisfied(task)

                if deps_satisfied:
                    logger.info(
                        f"🚀 Task {task.id} (name: {task.name}) dependencies now satisfied, executing"
                    )
                    triggered_tasks.append(task)
                    try:
                        await self._execute_single_task(task, use_callback=True)
                    except Exception as e:
                        # Log business errors without stack trace, unexpected errors with stack trace
                        if isinstance(e, BusinessError):
                            logger.error(
                                f"❌ Business error executing dependent task {task.id}: {str(e)}"
                            )
                        else:
                            logger.error(
                                f"❌ Failed to execute dependent task {task.id}: {str(e)}",
                                exc_info=True,
                            )
                        # Update task status using repository
                        await self.task_repository.transition_task(
                            task_id=task.id,
                            expected_statuses=["pending", "in_progress"],
                            expected_attempt_id=task.attempt_id,
                            status="failed",
                            error=str(e),
                            error_info=execution_error_info(
                                e,
                                task_id=str(task.id),
                                attempt_id=task.attempt_id,
                                phase="dependency_trigger",
                            ),
                        )
                else:
                    logger.debug(
                        f"Task {task.id} (name: {task.name}) dependencies not yet satisfied"
                    )

            if triggered_tasks:
                logger.info(f"Successfully triggered {len(triggered_tasks)} dependent tasks")
            else:
                logger.debug("No tasks were triggered by this completion")
        else:
            logger.debug("No waiting tasks found")

    def _record_breaker_failure(self, task: TaskModel, error: BaseException) -> None:
        """Record an executor fault on the circuit breaker.

        A BusinessError (ValidationError / ConfigurationError / etc.) is a client
        input problem, NOT an executor-health signal. Counting it toward the breaker
        would trip it OPEN and reject otherwise-healthy tasks for that executor, so
        only genuine executor/system faults are recorded.
        """
        if not self._circuit_breaker_registry or isinstance(error, BusinessError):
            return
        executor_id = self._get_executor_id(task)
        if executor_id:
            self._circuit_breaker_registry.get(executor_id).record_failure()

    def _get_executor_id(self, task: TaskModel) -> Optional[str]:
        """
        Get executor ID for the task from params or schemas

        Args:
            task: Task to get executor ID for

        Returns:
            Executor ID if found, None otherwise
        """
        # Check params first
        params = task.params or {}
        schemas = task.schemas or {}
        return params.get("executor_id") or schemas.get("method")

    def _load_executor(self, task: TaskModel, inputs: Optional[Dict[str, Any]] = None) -> Extension:
        """
        Load executor instance for the task and store in _executor_instances

        Args:
            task: Task to load executor for
        """

        executor_id = self._get_executor_id(task)
        if not executor_id:
            raise ExecutorError(f"Executor ID not specified for task {task.id}")

        from apflow.bridge.access import authorize_capability, execution_context

        context = execution_context.get()
        attributes = getattr(getattr(context, "identity", None), "attrs", {})
        namespace = getattr(context, "data", {}).get(
            "apflow_namespace", attributes.get("apflow_namespace", "apflow")
        )
        authorize_capability(f"{namespace}.{executor_id}", context, inputs=inputs)

        # Check permission BEFORE loading executor to avoid unnecessary work
        # and provide clearer error messages
        if not self._check_executor_permission(executor_id):
            error_msg = f"Executor '{executor_id}' is not allowed by APFLOW_EXTENSIONS configuration for task {task.id}"
            raise ExecutorError(error_msg)

        # Get executor from unified extension registry
        registry = get_registry()
        extension = registry.get_by_id(executor_id)
        if not extension:
            from apflow.core.extensions.manager import load_extension_by_id

            # Try to load extension by executor_id
            load_extension_by_id(executor_id)

            # Get executor again after loading
            extension = registry.get_by_id(executor_id)
            if not extension:
                raise ExecutorError(
                    f"Executor '{executor_id}' could not be loaded for task {task.id}"
                )

        if extension.category != ExtensionCategory.EXECUTOR:
            raise ExecutorError(
                f"Executor '{executor_id}' ({extension.category}) is not an executor for task {task.id}"
            )

        return extension

    async def _watch_cancellation(
        self, task_id: str, event: asyncio.Event, attempt_id: int
    ) -> None:
        """Observe persisted requests using an independent session on this database."""

        async def inspect_request(session: Union[Session, AsyncSession]) -> None:
            repository = TaskRepository(session)
            task = await repository.get_task_by_id(task_id)
            if task is None or task.attempt_id != attempt_id:
                return
            if task.cancel_requested_at is not None and not event.is_set():
                manager = TaskManager(
                    session,
                    executor_instances=self._executor_instances,
                    cancellation_events=self._cancellation_events,
                )
                await manager.cancel_task(task_id)

        while not event.is_set():
            await asyncio.sleep(0.1)
            try:
                if isinstance(self.db, AsyncSession):
                    async with AsyncSession(bind=self.db.bind) as session:
                        await inspect_request(session)
                else:
                    with Session(bind=self.db.get_bind()) as session:
                        await inspect_request(session)
            except Exception as exc:
                logger.error("Cannot inspect cancellation for task %s: %s", task_id, exc)
                return

    async def _execute_attempt(self, task: TaskModel, inputs: Dict[str, Any]) -> ExecutorResult:
        """Record and account every invocation, including failed and cancelled work."""
        from apflow.durability.attempts import AttemptRecorder

        expected = self._execution_attempt_ids.get(str(task.id), task.attempt_id)
        current = await self._check_cancellation_and_refresh_task(str(task.id), "before attempt")
        if current is None or current.attempt_id != expected:
            return {"result": None}
        task_id, attempt_id = str(task.id), task.attempt_id
        number = self._attempt_numbers.get(task_id, 0) + 1
        self._attempt_numbers[task_id] = number
        recorder = AttemptRecorder(self.db)
        record_id = await recorder.start(task_id, attempt_id, number)
        self._attempt_records[task_id] = record_id
        token = current_invocation_id.set(record_id)
        try:
            return await self._execute_recorded_invocation(task, inputs, record_id)
        finally:
            current_invocation_id.reset(token)

    async def _execute_recorded_invocation(
        self, task: TaskModel, inputs: Dict[str, Any], record_id: str
    ) -> ExecutorResult:
        from apflow.durability.attempts import AttemptRecorder

        recorder = AttemptRecorder(self.db)
        task_id, attempt_id = str(task.id), task.attempt_id
        try:
            result = await self._execute_task_with_schemas(task, inputs)
        except BaseException as exc:
            usage = getattr(exc, "token_usage", None)
            info = execution_error_info(exc, task_id=task_id, attempt_id=attempt_id)
            status = "unknown" if info["delivery_state"] == "unknown" else "failed"
            if isinstance(exc, asyncio.CancelledError):
                status = "cancelled" if task.cancel_requested_at is not None else "unknown"
            await recorder.finish(record_id, status, error_info=info, token_usage=usage)
            raise
        usage = result.get("token_usage")
        info = result.get("error_info")
        if result.get("error") and info is None:
            info = execution_error_info(
                ExecutorError(str(result.get("error"))), task_id=task_id, attempt_id=attempt_id
            )
            result["error_info"] = info
        latest = await self.task_repository.get_task_by_id(task_id)
        cancelled = latest is not None and (
            latest.status == "cancelled" or latest.cancel_requested_at is not None
        )
        status = "cancelled" if cancelled else "failed" if result.get("error") else "completed"
        await recorder.finish(record_id, status, error_info=info, token_usage=usage)
        return result

    async def _execute_task_with_schemas(
        self, task: TaskModel, inputs: Dict[str, Any]
    ) -> ExecutorResult:
        """
        Execute task based on schemas configuration

        Uses the executor registry to find and instantiate the appropriate executor
        based on task_type in schemas. Supports both built-in and third-party executors.

        Args:
            task: Task to execute
            inputs: Input parameters for task execution

        Returns:
            Task execution result

        Raises:
            ValueError: If task_type is not registered in executor registry
        """
        schemas = task.schemas or {}
        task_type = schemas.get("type")  # Optional: only used if method is not an executor id
        task_method = self._get_executor_id(task)
        params = task.params or {}
        # ============================================================
        # 1. load extension based on executor_id in params (highest priority)
        # ============================================================
        try:
            extension = self._load_executor(task, inputs)
            executor_id = extension.id
            # Log after we have executor_id info
            logger.info(
                f"Executing task {task.id} with type={task_type}, method={task_method}, executor_id={executor_id}"
            )
            logger.debug(f"Task {task.id} params: {params}, executor_id from params: {executor_id}")

        except ExecutorError as e:
            error_msg = f"{str(e)}"
            logger.error(f"Executor loading error for task {task.id}: {str(e)}")
            result = {
                "result": None,
                "error": error_msg,
                "task_id": task.id,
                "name": task.name,
                "task_type": task_type,
                "task_params": task_method,
                "inputs": inputs,
            }
            # If permission error, include allowed executors
            if "not allowed" in error_msg:
                from apflow.core.extensions.manager import get_allowed_executor_ids

                allowed = get_allowed_executor_ids()
                if allowed is not None:
                    result["allowed_executors"] = list(allowed)
            return {
                "result": result,
                "error": error_msg,
                "error_info": _preflight_failure_info(
                    task, "EXECUTOR_UNAVAILABLE", "configuration"
                ),
            }

        # ============================================================
        # 2. extract executor initialization parameters from params
        # ============================================================
        # all fields in params except executor_id are initialization parameters
        init_params = params.copy()
        init_params.pop("executor_id", None)  # executor_id is already handled separately

        # Get input_schema from schemas to pass to executor
        input_schema = schemas.get("input_schema")
        if input_schema:
            init_params["inputs_schema"] = input_schema

        # Get model from schemas to pass to executor
        model = schemas.get("model")
        if model:
            init_params["model"] = model

        # ============================================================
        # 3. create executor instance
        # ============================================================
        # Create cancellation checker
        cancellation_event = self._cancellation_events.setdefault(str(task.id), asyncio.Event())
        if task.status == "cancelled" or task.cancel_requested_at is not None:
            cancellation_event.set()

        def cancellation_checker() -> bool:
            return cancellation_event.is_set()

        # Create executor: inputs as inputs parameter, other as **kwargs
        # Note: Input validation is now handled by executor itself (in BaseTask or executor.execute)
        # Pass task context to executor for full access to task information (including custom fields)
        # This allows executors to:
        # - Access all task fields (including custom TaskModel fields)
        # - Modify task context (e.g., update status, progress, custom fields)
        # - Share task object efficiently (lifecycle managed by TaskManager)
        registry = get_registry()

        # Extract executor config from task.params for initialization
        executor_config = params.copy()
        executor_config.pop("executor_id", None)  # executor_id is already handled separately

        executor = registry.create_executor_instance(
            extension_id=executor_id,
            inputs=inputs,  # inputs for execution (will be validated by executor)
            task=task,  # Pass task context (TaskModel instance) - supports custom TaskModel classes
            user_id=task.user_id,  # Also pass user_id for backward compatibility
            **executor_config,  # Pass params as kwargs for executor initialization
            cancellation_checker=cancellation_checker,
        )

        if executor is None:
            error_msg = f"Failed to create executor instance '{executor_id}'"
            logger.error(error_msg)
            return {
                "result": None,
                "error": error_msg,
                "error_info": _preflight_failure_info(
                    task, "EXECUTOR_INITIALIZATION_FAILED", "configuration"
                ),
            }

        # Store executor for cancellation support
        if hasattr(executor, "cancel"):
            async with self._executor_lock:
                self._executor_instances[task.id] = executor
            logger.debug(f"Stored executor instance for task {task.id} (supports cancellation)")

        # --- Durability: restore executor state via the checkpoint contract ---
        # Loaded earlier into inputs["_checkpoint"] (see _execute_single_task);
        # invoke it through the documented resume_from_checkpoint()/
        # supports_checkpoint() interface, which previously was never called.
        checkpoint_data = inputs.get("_checkpoint") if inputs else None
        if checkpoint_data is not None and executor.supports_checkpoint():
            await executor.resume_from_checkpoint(checkpoint_data)
            logger.info(f"Task {task.id}: restored executor state from checkpoint")

        # ============================================================
        # 4. execute executor (with hooks)
        # ============================================================
        # Note: Input validation and any executor-specific input processing
        # should be handled by the executor itself (in BaseTask or executor.execute)
        # TaskManager only handles task orchestration and distribution

        # Check for demo mode (use instance variable instead of inputs)
        if self.use_demo:
            logger.info(f"Demo mode enabled for task {task.id} with executor {executor_id}")
            # Send task start event for streaming
            if self.stream:
                self.streaming_callbacks.task_start(task.id)

            # Try to get custom demo result from executor if available
            demo_result = None
            if hasattr(executor, "get_demo_result"):
                try:
                    demo_result = executor.get_demo_result(task, inputs)
                except Exception as e:
                    logger.warning(
                        f"get_demo_result() failed for executor {executor_id}: {str(e)}. Using default demo data."
                    )

            # Use default demo result if executor doesn't provide one
            if demo_result is None:
                demo_result = {"result": "Demo execution result", "demo_mode": True}

            # Ensure result format is consistent
            if isinstance(demo_result, dict):
                demo_result["demo_mode"] = True

                # Handle demo sleep time
                # Executor can specify _demo_sleep in get_demo_result() return value
                # Global scale factor is applied to executor's sleep time
                sleep_seconds = 0.0
                if "_demo_sleep" in demo_result:
                    # Executor-specific sleep time (remove from result)
                    executor_sleep = float(demo_result.pop("_demo_sleep"))
                    # Apply global scale factor
                    from apflow.core.config import get_demo_sleep_scale

                    scale = get_demo_sleep_scale()
                    sleep_seconds = executor_sleep * scale
                    logger.debug(
                        f"Demo mode: executor sleep={executor_sleep}s, scale={scale}, final sleep={sleep_seconds}s"
                    )

                # Sleep to simulate execution time if configured
                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)

                return {"result": demo_result}
            return {"result": demo_result}

        # Get executor class to check for hooks
        executor_class = type(executor)

        # Call executor-specific pre_hook if available
        hook_result = None
        if hasattr(executor_class, "_executor_hooks"):
            pre_hook = executor_class._executor_hooks.get("pre_hook")
            if pre_hook:
                try:
                    logger.debug(f"Calling pre_hook for executor {executor_id} on task {task.id}")
                    if iscoroutinefunction(pre_hook):
                        hook_result = await pre_hook(executor, task, inputs)
                    else:
                        hook_result = await asyncio.to_thread(pre_hook, executor, task, inputs)

                    # If hook returned a result, skip executor execution
                    if hook_result is not None:
                        logger.info(
                            f"Pre_hook returned result for executor {executor_id}, skipping execution"
                        )
                        # Call post_hook if available
                        post_hook = executor_class._executor_hooks.get("post_hook")
                        if post_hook:
                            try:
                                if iscoroutinefunction(post_hook):
                                    await post_hook(executor, task, inputs, hook_result)
                                else:
                                    await asyncio.to_thread(
                                        post_hook, executor, task, inputs, hook_result
                                    )
                            except Exception as e:
                                logger.warning(
                                    f"Post_hook failed for executor {executor_id}: {str(e)}"
                                )
                except Exception as e:
                    logger.warning(
                        f"Pre_hook failed for executor {executor_id}: {str(e)}. Continuing with execution."
                    )

        if hook_result is not None:
            return normalize_executor_result(hook_result, executor.result_format)

        try:
            current = await self.task_repository.get_task_by_id(str(task.id))
            if current is not None and current.cancel_requested_at is not None:
                cancellation_event.set()
            if cancellation_event.is_set():
                return {"result": None}

            async def invoke_executor() -> Any:
                watcher = asyncio.create_task(
                    self._watch_cancellation(str(task.id), cancellation_event, task.attempt_id)
                )
                try:
                    return await executor.execute(inputs)
                finally:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)

            if task.budget_scope_id:
                from apflow.governance.budget import BudgetManager

                budget_manager = self._budget_manager or BudgetManager(self.task_repository)
                bound_method = getattr(executor, "token_reservation_bound", None)
                raw_bound = bound_method(inputs) if callable(bound_method) else None
                bound = raw_bound if isinstance(raw_bound, int) else None
                result = await budget_manager.execute_with_reservation(
                    task, invoke_executor, token_bound=bound, result_format=executor.result_format
                )
            else:
                result = await invoke_executor()

            # Call executor-specific post_hook if available
            if hasattr(executor_class, "_executor_hooks"):
                post_hook = executor_class._executor_hooks.get("post_hook")
                if post_hook:
                    try:
                        logger.debug(
                            f"Calling post_hook for executor {executor_id} on task {task.id}"
                        )
                        if iscoroutinefunction(post_hook):
                            await post_hook(executor, task, inputs, result)
                        else:
                            await asyncio.to_thread(post_hook, executor, task, inputs, result)
                    except Exception as e:
                        logger.warning(f"Post_hook failed for executor {executor_id}: {str(e)}")

            # Explicitly clear task context to prevent memory leaks
            # This is important for long-running executors and memory management
            if hasattr(executor, "clear_task_context"):
                executor.clear_task_context()
                logger.debug(f"Cleared task context for executor {executor_id} on task {task.id}")

            # Check for separate token usage stored by executor
            if hasattr(executor, "_last_token_usage") and executor._last_token_usage:
                logger.info(
                    f"Token usage from executor {executor_id}: {executor._last_token_usage}"
                )
                # TODO: Store token usage in a separate statistics system or database

            return normalize_executor_result(result, executor.result_format)
        except Exception as e:
            # Log business errors without stack trace, unexpected errors with stack trace
            if isinstance(e, BusinessError):
                logger.error(
                    f"Business error executing task {task.id} with executor {executor.__class__.__name__}: {e}"
                )
            else:
                logger.error(
                    f"Error executing task {task.id} with executor {executor.__class__.__name__}: {e}",
                    exc_info=True,
                )
            # Note: the executor reference and its task context are deliberately
            # NOT cleared here. A retry callback (see _execute_single_task's
            # _on_retry) needs to read this same instance via get_checkpoint()
            # between attempts; clearing it on every per-attempt failure made
            # checkpoint-on-retry permanently unreachable. Final cleanup happens
            # once the whole retry sequence resolves, in _execute_single_task.
            # Re-raise the exception to let TaskManager mark the task as failed
            raise

    async def resolve_task_dependencies(self, task: TaskModel) -> Dict[str, Any]:
        """Inject stored business outputs by dependency ID without schema guessing."""
        declared_inputs = cast(Optional[Dict[str, Any]], task.inputs)
        inputs = deepcopy(declared_inputs) if declared_inputs else {}
        dependencies = cast(Optional[List[Any]], task.dependencies)
        if not dependencies:
            return inputs
        completed_tasks = await self.task_repository.get_completed_tasks_by_id(task)
        for dependency in dependencies:
            dep_id = dependency.get("id") if isinstance(dependency, dict) else dependency
            if not isinstance(dep_id, str):
                continue
            required = dependency.get("required", True) if isinstance(dependency, dict) else True
            source = completed_tasks.get(dep_id)
            if source is None:
                if required:
                    raise ValidationError(
                        f"Required dependency '{dep_id}' is not completed for task {task.id}"
                    )
                inputs.pop(dep_id, None)
                continue
            effective = await self.task_repository.resolve_effective_task(source)
            inputs[dep_id] = deepcopy(effective.result)
        return inputs


__all__ = ["TaskManager"]
