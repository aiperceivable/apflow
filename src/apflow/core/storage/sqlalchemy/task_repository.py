"""
Task repository for task database operations

This module provides a TaskRepository class that encapsulates all database operations
for tasks. TaskManager should use TaskRepository instead of directly operating on db session.
"""

from asyncio import Task
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update
from typing import List, Dict, Any, Optional, Union, TYPE_CHECKING, Type, Set
from apflow.core.storage.sqlalchemy.models import TaskModel, TaskOriginType, TaskModelType
from apflow.core.execution.errors import ValidationError
from sqlalchemy_session_proxy import SqlalchemySessionProxy
from apflow.logger import get_logger

if TYPE_CHECKING:
    from apflow.core.types import TaskTreeNode

logger = get_logger(__name__)


class TaskRepository:
    """
    Task repository for database operations

    Provides methods for:
    - Creating, updating, and deleting tasks
    - Building task trees
    - Querying tasks by various criteria
    - Managing task hierarchies

    TaskManager should use this repository instead of directly operating on db session.

    Supports custom TaskModel classes via task_model_class parameter.
    Users can pass their custom TaskModel subclass to support custom fields.

    Example:
        # Use default TaskModel
        repo = TaskRepository(db)

        # Use custom TaskModel with additional fields
        repo = TaskRepository(db, task_model_class=MyTaskModel)
        task = await repo.create_task(..., project_id="proj-123")  # Custom field
    """

    def __init__(
        self, db: Union[Session, AsyncSession], task_model_class: Type[TaskModelType] = TaskModel
    ):
        """
        Initialize TaskRepository

        Args:
            db: Database session (sync or async)
            task_model_class: Custom TaskModel class (default: TaskModel)
                Users can pass their custom TaskModel subclass that inherits TaskModel
                to add custom fields (e.g., project_id, department, etc.)
                Example: TaskRepository(db, task_model_class=MyTaskModel)
        """
        self.db = SqlalchemySessionProxy(db)
        self.task_model_class = task_model_class

    def build_task(self, **kwargs: Any) -> TaskModelType:
        """
        Build a TaskModel instance without saving to database

        Args:
            **kwargs: Fields to set on the task

        Returns:
            TaskModel instance (or custom TaskModel subclass if configured)
        """
        task = self.task_model_class.create(kwargs)
        return task

    async def create_task(
        self, name: str, **kwargs  # User-defined custom fields (e.g., project_id, department, etc.)
    ) -> TaskModelType:
        """
        Create a new task

        Args:
            name: Task name
            **kwargs: These fields will be set on the task if they exist as columns in the TaskModel

        Returns:
            Created TaskModel instance (or custom TaskModel subclass if configured)
        """

        # Create task instance using configured TaskModel class
        task = self.task_model_class.create({"name": name, **kwargs})

        self.db.add(task)

        try:
            await self.db.commit()
            await self.db.refresh(task)
        except Exception as e:
            await self.db.rollback()
            logger.error(f"Error creating task: {str(e)}")
            raise

        return task

    async def get_task_by_id(self, task_id: str) -> Optional[TaskModelType]:
        """
        Get a task by ID using standard ORM query

        Args:
            task_id: Task ID

        Returns:
            TaskModel instance (or custom TaskModel subclass) or None if not found
        """
        try:
            stmt = select(self.task_model_class).where(self.task_model_class.id == task_id)
            result = await self.db.execute(stmt)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting task by id {task_id}: {str(e)}")
            raise

    async def get_child_tasks_by_parent_id(self, parent_id: str) -> List[TaskModelType]:
        """
        Get child tasks by parent ID

        Args:
            parent_id: Parent task ID

        Returns:
            List of child TaskModel instances (or custom TaskModel subclass), ordered by priority
        """
        try:
            stmt = (
                select(self.task_model_class)
                .filter(self.task_model_class.parent_id == parent_id)
                .order_by(self.task_model_class.priority.asc())
            )
            result = await self.db.execute(stmt)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting child tasks for parent {parent_id}: {str(e)}")
            return []

    async def get_root_task(self, task: TaskModelType) -> TaskModelType:
        """
        Get root task (traverse up the tree until parent_id is None)

        Args:
            task: Starting task

        Returns:
            Root TaskModel instance (or custom TaskModel subclass)
        """
        current_task = task
        visited_ids: Set[str] = {str(task.id)}

        # Traverse up to find root with cycle detection
        while current_task.parent_id:
            parent_id = str(current_task.parent_id)

            # Cycle detection: if we've seen this parent_id before, break to avoid infinite loop
            if parent_id in visited_ids:
                logger.warning(
                    f"Cycle detected in task tree: task {current_task.id} has parent {parent_id} "
                    f"which was already visited. Breaking to prevent infinite loop."
                )
                break

            visited_ids.add(parent_id)
            parent = await self.get_task_by_id(current_task.parent_id)
            if not parent:
                break
            current_task = parent

        return current_task

    async def get_all_tasks_in_tree(self, root_task: TaskModelType) -> List[TaskModelType]:
        """
        Get all tasks in the task tree (recursive)

        Args:
            root_task: Root task of the tree

        Returns:
            List of all tasks in the tree (or custom TaskModel subclass)
        """

        task_tree_id = getattr(root_task, "task_tree_id", None)
        is_root_task = root_task.parent_id is None
        if is_root_task and task_tree_id:
            try:
                # Fast path: single query to get all tasks in tree
                all_tasks = await self.get_tasks_by_tree_id(task_tree_id)
                logger.debug(f"✅ [OPTIMIZED] Used task_tree_id fast path for task {root_task.id}")
                return all_tasks
            except (ValueError, AttributeError) as e:
                # task_tree_id exists but get_tasks_by_tree_id failed (data inconsistency)
                # Fall back to slow path
                logger.warning(
                    f"⚠️ [FALLBACK] Fast path failed for task {root_task.id} with task_tree_id={task_tree_id}: {str(e)}. "
                    f"Falling back to slow path (get_root_task + build_task_tree)."
                )

        all_tasks = [root_task]

        # Get all child tasks recursively
        async def get_children(parent_id: str):
            children = await self.get_child_tasks_by_parent_id(parent_id)
            for child in children:
                all_tasks.append(child)
                await get_children(child.id)

        await get_children(root_task.id)
        return all_tasks

    async def get_tasks_by_tree_id(self, tree_id: str) -> List[TaskModelType]:
        """
        Get tasks by task tree ID.
        All tasks in the same task tree share the same tree_id value,
        which allows efficient querying and tree reconstruction without traversing
        parent_id relationships.

        This method is optimized for recursive query scenarios where you need to
        get all tasks in a tree with a single query instead of recursive parent_id
        traversals.

        Note:
            This method does NOT validate root task existence. If you need to ensure
            a root task exists (e.g., for tree building), validate it yourself:
            parent_task = [task for task in tasks if task.parent_id is None]

        Args:
            tree_id: The tree_id value shared by all tasks in the tree.

        Returns:
            List of all tasks with the given tree_id (may include root task or not)

        """
        # CRITICAL: Use expire_all() before query to ensure fresh data in concurrent environments
        # This prevents stale data when other servers update task status in load-balanced environments
        # Avoid using refresh() as it can hang if there are database locks
        self.db.expire_all()
        stmt = (
            select(self.task_model_class)
            .filter(self.task_model_class.task_tree_id == tree_id)
            .order_by(self.task_model_class.priority.asc())
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def build_task_tree_by_tree_id(self, tree_id: str) -> "TaskTreeNode":
        """
        Build a complete task tree structure by tree_id.

        All tasks in the same task tree share the same tree_id value,
        which allows efficient querying and tree reconstruction without traversing
        parent_id relationships.

        Process:
        1. Query all tasks with the given tree_id
        2. Validate that tasks exist and root task is found
        3. Build tree structure recursively starting from root task

        Args:
            tree_id: The tree_id value shared by all tasks in the tree.

        Returns:
            TaskTreeNode representing the root of the task tree with all children recursively built.

        Note:
            This method assumes all tasks with the same tree_id belong to the same tree.
        """
        # Get all tasks in tree - get_tasks_by_tree_id validates data integrity
        # but does not validate root task existence (for flexibility)
        # CRITICAL: get_tasks_by_tree_id now uses expire_all() to ensure fresh data
        # in concurrent environments, but we should still refresh tasks before building tree
        # to ensure we have the absolute latest state from database
        tasks = await self.get_tasks_by_tree_id(tree_id)

        # CRITICAL: Refresh all tasks to ensure we have latest state from database
        # This is important in concurrent environments where task status may have changed
        # between query and tree building
        for task in tasks:
            try:
                await self.db.refresh(task)
            except Exception as refresh_error:
                # If refresh fails (e.g., task was deleted), log and continue
                logger.warning(
                    f"Failed to refresh task {task.id} in build_task_tree_by_tree_id: {refresh_error}"
                )

        # Find root task - required for tree building
        root_task = [task for task in tasks if task.parent_id is None]
        if not root_task:
            logger.error(f"Root task not found for tree_id {tree_id}")
            raise ValidationError(f"Root task not found for tree_id {tree_id}")

        # Lazy import to avoid circular dependency
        from apflow.core.types import TaskTreeNode

        # Build tree structure starting from root task
        task_tree = TaskTreeNode(task=root_task[0])

        def add_children(task: Task, task_tree: TaskTreeNode):
            """
            Recursively add child tasks to the tree structure.

            For each task in the flat list, if it has the current task as parent,
            add it as a child node. If the child has children (has_children=True),
            recursively build its subtree before adding.

            Args:
                task: Current parent task to find children for
                task_tree: Current tree node to add children to
            """
            for child in tasks:
                # Check if this task is a child of the current parent task
                if str(child.parent_id) == str(task.id):
                    if bool(child.has_children):
                        # Child has children: create subtree and recursively add grandchildren
                        child_task_tree = TaskTreeNode(task=child)
                        add_children(child, child_task_tree)
                        # CRITICAL: Add the child subtree to the parent tree
                        # This was missing in the original implementation
                        task_tree.add_child(child_task_tree)
                    else:
                        # Leaf node: add directly without recursion
                        task_tree.add_child(TaskTreeNode(task=child))

        # Start recursive tree building from root task
        add_children(root_task[0], task_tree)

        return task_tree

    async def build_task_tree(self, task: TaskModelType) -> "TaskTreeNode":
        """
        Build TaskTreeNode for a task with its children (recursive)

        Args:
            task: Root task (or custom TaskModel subclass)

        Returns:
            TaskTreeNode instance with all children recursively built
        """
        task_tree_id = getattr(task, "task_tree_id", None)
        is_root_task = task.parent_id is None
        if is_root_task and task_tree_id:
            try:
                # Fast path: single query to get all tasks in tree
                task_tree = await self.build_task_tree_by_tree_id(task_tree_id)
                logger.debug(f"✅ [OPTIMIZED] Used task_tree_id fast path for task {task.id}")
                return task_tree
            except (ValueError, AttributeError) as e:
                # task_tree_id exists but tree build failed (data inconsistency)
                # Fall back to slow path
                logger.warning(
                    f"⚠️ [FALLBACK] Fast path failed for task {task.id} with task_tree_id={task_tree_id}: {str(e)}. "
                    f"Falling back to slow path (get_root_task + build_task_tree)."
                )

        # Lazy import to avoid circular dependency
        from apflow.core.types import TaskTreeNode

        # Get all child tasks
        child_tasks = await self.get_child_tasks_by_parent_id(task.id)

        # Create the main task node
        task_node = TaskTreeNode(task=task)

        # Add child tasks recursively
        for child_task in child_tasks:
            child_node = await self.build_task_tree(child_task)
            task_node.add_child(child_node)

        return task_node

    async def update_task(self, task_id: str, **fields: Any) -> TaskModelType:
        """
        Update arbitrary fields of a task.

        TODO: check dependencies and validate for task tree if dependencies exist

        Args:
            task_id: Task ID
            **fields: Fields to update (e.g., status="completed", inputs={...})

        Returns:
            True if successful, False if task not found
        """
        try:
            task = await self.get_task_by_id(task_id)
            if not task:
                raise ValueError(f"Task with id {task_id} not found")

            task.update_from_dict(fields)

            for key, _ in fields.items():
                if hasattr(task, key):
                    # Mark JSON fields as modified for SQLAlchemy change detection
                    if task.is_json_field(key):
                        flag_modified(task, key)

            await self.db.commit()
            await self.db.refresh(task)
            logger.info(f"Updated task {task_id} fields: {list(fields.keys())}")
            return task

        except Exception as e:
            logger.error(f"Error updating task {task_id}: {str(e)}")
            await self.db.rollback()
            raise

    async def try_claim_for_execution(
        self, task_id: str, allowed_statuses: Optional[List[str]] = None
    ) -> Optional[TaskModelType]:
        """
        Atomically transition a task from an allowed status to in_progress.

        Uses a conditional UPDATE (compare-and-swap) to prevent TOCTOU races:
        only one caller succeeds when multiple coroutines or workers try to
        claim the same task concurrently.

        Args:
            task_id: Task ID to claim
            allowed_statuses: Statuses from which transition is allowed.
                Defaults to ["pending"] if not specified.

        Returns:
            The refreshed task if the claim succeeded, None if another caller
            already transitioned the task (0 rows matched).
        """
        if allowed_statuses is None:
            allowed_statuses = ["pending"]

        try:
            from datetime import timezone as tz

            now = datetime.now(tz.utc)
            stmt = (
                update(self.task_model_class)
                .where(
                    self.task_model_class.id == task_id,
                    self.task_model_class.status.in_(allowed_statuses),
                )
                .values(status="in_progress", started_at=now, error=None)
            )
            result = await self.db.execute(stmt)
            await self.db.commit()

            rows_affected = result.rowcount  # type: ignore[union-attr]
            if rows_affected == 0:
                logger.info(
                    f"Task {task_id} claim failed: status not in {allowed_statuses} "
                    f"(already claimed by another caller)"
                )
                return None

            # Re-fetch the task to return a fully loaded ORM instance
            task = await self.get_task_by_id(task_id)
            logger.info(f"Task {task_id} claimed for execution (status → in_progress)")
            return task

        except Exception as e:
            logger.error(f"Error claiming task {task_id}: {str(e)}")
            await self.db.rollback()
            raise

    async def get_completed_tasks_by_id(self, task: TaskModelType) -> Dict[str, TaskModelType]:
        """
        Get all completed tasks in the same task tree by id

        Args:
            task: Task to get sibling tasks for

        Returns:
            Dictionary mapping task ids to completed TaskModelType instances
        """
        # Get root task to find all tasks in the tree
        root_task = await self.get_root_task(task)

        # Get all tasks in the tree
        all_tasks = await self.get_all_tasks_in_tree(root_task)

        # Filter completed tasks with results
        completed_tasks = [t for t in all_tasks if t.status == "completed" and t.result is not None]

        # Create a map of completed tasks by id
        completed_tasks_by_id = {t.id: t for t in completed_tasks}

        return completed_tasks_by_id

    async def get_completed_tasks_by_ids(self, task_ids: List[str]) -> Dict[str, TaskModelType]:
        """
        Get completed tasks by a list of IDs

        Args:
            task_ids: List of task IDs

        Returns:
            Dictionary mapping task_id to TaskModel (or custom TaskModel subclass) for completed tasks
        """
        if not task_ids:
            return {}

        try:
            stmt = select(self.task_model_class).filter(
                self.task_model_class.id.in_(task_ids), self.task_model_class.status == "completed"
            )
            result = await self.db.execute(stmt)
            tasks = result.scalars().all()
            return {task.id: task for task in tasks}

        except Exception as e:
            logger.error(f"Error getting completed tasks by IDs: {str(e)}")
            return {}

    async def query_tasks(
        self,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        parent_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "created_at",
        order_desc: bool = True,
    ) -> List[TaskModelType]:
        """
        Query tasks with filters and pagination

        Args:
            user_id: Optional user ID filter
            status: Optional status filter (e.g., "completed", "pending", "in_progress", "failed")
            parent_id: Optional parent ID filter. If None, no filter. If empty string "", filter for root tasks (parent_id is None)
            limit: Maximum number of tasks to return (default: 100)
            offset: Number of tasks to skip (default: 0)
            order_by: Field to order by (default: "created_at")
            order_desc: If True, order descending; if False, order ascending (default: True)

        Returns:
            List of TaskModel instances (or custom TaskModel subclass) matching the criteria
        """
        try:
            # Build query
            stmt = select(self.task_model_class)

            # Apply filters
            if user_id is not None:
                stmt = stmt.filter(self.task_model_class.user_id == user_id)

            if status is not None:
                stmt = stmt.filter(self.task_model_class.status == status)

            # Apply parent_id filter
            if parent_id is not None:
                if parent_id == "":
                    # Empty string means filter for root tasks (parent_id is None)
                    stmt = stmt.filter(self.task_model_class.parent_id.is_(None))
                else:
                    # Specific parent_id
                    stmt = stmt.filter(self.task_model_class.parent_id == parent_id)

            # Apply ordering
            order_column = getattr(self.task_model_class, order_by, None)
            if order_column is not None:
                if order_desc:
                    stmt = stmt.order_by(order_column.desc())
                else:
                    stmt = stmt.order_by(order_column.asc())

            # Apply pagination
            stmt = stmt.offset(offset).limit(limit)
            result = await self.db.execute(stmt)
            tasks = result.scalars().all()

            return list(tasks)

        except Exception as e:
            logger.error(f"Error querying tasks: {str(e)}")
            return []

    async def query_tasks_by_statuses(
        self,
        statuses: List[str],
        user_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "created_at",
        order_desc: bool = True,
    ) -> List[TaskModelType]:
        """
        Query tasks by multiple statuses

        Args:
            statuses: List of status values to filter by
            user_id: Optional user ID filter
            limit: Maximum number of tasks to return
            offset: Number of tasks to skip
            order_by: Field to order by
            order_desc: If True, order descending

        Returns:
            List of TaskModel instances matching any of the statuses
        """
        try:
            from sqlalchemy import select

            stmt = select(self.task_model_class)

            # Apply filters
            if user_id is not None:
                stmt = stmt.filter(self.task_model_class.user_id == user_id)

            # Filter by statuses (IN clause)
            if statuses:
                stmt = stmt.filter(self.task_model_class.status.in_(statuses))

            # Apply ordering
            order_column = getattr(self.task_model_class, order_by, None)
            if order_column is not None:
                if order_desc:
                    stmt = stmt.order_by(order_column.desc())
                else:
                    stmt = stmt.order_by(order_column.asc())

            # Apply pagination
            stmt = stmt.offset(offset).limit(limit)
            result = await self.db.execute(stmt)
            tasks = result.scalars().all()

            return list(tasks)

        except Exception as e:
            logger.error(f"Error querying tasks by statuses: {str(e)}")
            return []

    async def count_tasks_by_status(
        self,
        statuses: List[str],
        user_id: Optional[str] = None,
        root_only: bool = False,
    ) -> Dict[str, int]:
        """
        Count tasks grouped by status using efficient SQL COUNT queries.

        Args:
            statuses: List of status values to count
            user_id: Optional user ID filter
            root_only: If True, only count root tasks (parent_id is None)

        Returns:
            Dict with counts per status and "total" key
        """
        try:
            from sqlalchemy import func

            base_filters = []
            if user_id is not None:
                base_filters.append(self.task_model_class.user_id == user_id)
            if root_only:
                base_filters.append(self.task_model_class.parent_id.is_(None))

            counts: Dict[str, int] = {}
            total = 0

            for status in statuses:
                stmt = select(func.count()).select_from(self.task_model_class)
                for filter_clause in base_filters:
                    stmt = stmt.where(filter_clause)
                stmt = stmt.where(self.task_model_class.status == status)

                result = await self.db.execute(stmt)
                count = result.scalar_one()
                counts[status] = int(count or 0)
                total += count or 0

            counts["total"] = int(total)
            return counts

        except Exception as e:
            logger.error(f"Error counting tasks: {str(e)}")
            return {"total": 0}

    async def reset_task_tree_for_reexecution(self, root_task_id: str) -> int:
        """
        Reset all child tasks in a tree to clean pending state for re-execution.

        This is called before a scheduled root task with children is re-executed.
        It resets each child task's status, result, error, timestamps, and progress
        so the tree starts fresh, just like a new tasks.execute call.

        The root task itself is NOT reset here — it is handled by mark_scheduled_task_running().

        Args:
            root_task_id: The root task ID of the tree

        Returns:
            Number of child tasks reset
        """
        try:
            root_task = await self.get_task_by_id(root_task_id)
            if not root_task:
                logger.warning(f"Root task {root_task_id} not found for tree reset")
                return 0

            all_tasks = await self.get_all_tasks_in_tree(root_task)

            reset_count = 0
            for task in all_tasks:
                # Skip the root task — it is handled by mark_scheduled_task_running
                if str(task.id) == str(root_task_id):
                    continue

                task.status = "pending"
                task.result = None
                task.error = None
                task.started_at = None
                task.completed_at = None
                task.progress = 0.0

                reset_count += 1

            if reset_count > 0:
                await self.db.commit()
                logger.info(
                    f"Reset {reset_count} child tasks in tree {root_task_id} for re-execution"
                )

            return reset_count

        except Exception as e:
            logger.error(f"Error resetting task tree for re-execution: {str(e)}")
            await self.db.rollback()
            raise

    # ========== Scheduling Methods (for external schedulers) ==========

    async def get_due_scheduled_tasks(
        self,
        before: Optional[datetime] = None,
        user_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[TaskModelType]:
        """
        Get scheduled tasks that are due for execution.

        This method is designed for external schedulers to query tasks that need
        to be executed. A task is considered "due" when:
        - schedule_enabled is True
        - next_run_at is not None and <= before (default: now)
        - status is not 'in_progress' (to avoid double-execution of running tasks)
        - max_runs is None or run_count < max_runs
        - schedule_end_at is None or schedule_end_at > now

        Args:
            before: Get tasks due before this time (default: current time)
            user_id: Optional filter by user ID
            limit: Maximum number of tasks to return (default: 100)

        Returns:
            List of TaskModel instances that are due for execution,
            ordered by next_run_at ascending (earliest first)
        """
        from datetime import timezone as tz

        try:
            if before is None:
                before = datetime.now(tz.utc)

            # Build query for due scheduled tasks
            # Schedule eligibility is determined by schedule_* fields.
            # Only exclude in_progress to avoid double-execution of running tasks.
            stmt = select(self.task_model_class).filter(
                self.task_model_class.schedule_enabled == True,  # noqa: E712
                self.task_model_class.next_run_at.isnot(None),
                self.task_model_class.next_run_at <= before,
                self.task_model_class.status != "in_progress",
            )

            # Filter by max_runs (if set, run_count must be less)
            # Note: SQLAlchemy doesn't have direct "or null" in filter,
            # so we use or_ for max_runs check
            from sqlalchemy import or_

            stmt = stmt.filter(
                or_(
                    self.task_model_class.max_runs.is_(None),
                    self.task_model_class.run_count < self.task_model_class.max_runs,
                )
            )

            # Filter by schedule_end_at (if set, must be in future)
            stmt = stmt.filter(
                or_(
                    self.task_model_class.schedule_end_at.is_(None),
                    self.task_model_class.schedule_end_at > before,
                )
            )

            # Optional user_id filter
            if user_id is not None:
                stmt = stmt.filter(self.task_model_class.user_id == user_id)

            # Order by next_run_at ascending (earliest first)
            stmt = stmt.order_by(self.task_model_class.next_run_at.asc())

            # Apply limit
            stmt = stmt.limit(limit)

            result = await self.db.execute(stmt)
            tasks = result.scalars().all()

            return list(tasks)

        except Exception as e:
            logger.error(f"Error getting due scheduled tasks: {str(e)}")
            return []

    async def get_scheduled_tasks(
        self,
        enabled_only: bool = True,
        user_id: Optional[str] = None,
        schedule_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[TaskModelType]:
        """
        Get all scheduled tasks (for listing/monitoring).

        Args:
            enabled_only: If True, only return enabled schedules (default: True)
            user_id: Optional filter by user ID
            schedule_type: Optional filter by schedule type
            status: Optional filter by task status (e.g. pending, running, completed)
            limit: Maximum number of tasks to return (default: 100)
            offset: Number of tasks to skip (default: 0)

        Returns:
            List of TaskModel instances with scheduling configured,
            ordered by next_run_at ascending
        """
        try:
            # Build query - must have schedule_type set
            stmt = select(self.task_model_class).filter(
                self.task_model_class.schedule_type.isnot(None)
            )

            if enabled_only:
                stmt = stmt.filter(self.task_model_class.schedule_enabled == True)  # noqa: E712

            if user_id is not None:
                stmt = stmt.filter(self.task_model_class.user_id == user_id)

            if schedule_type is not None:
                stmt = stmt.filter(self.task_model_class.schedule_type == schedule_type)

            if status is not None:
                stmt = stmt.filter(self.task_model_class.status == status)

            # Order by next_run_at ascending (nulls last)
            from sqlalchemy import nullslast

            stmt = stmt.order_by(nullslast(self.task_model_class.next_run_at.asc()))

            # Apply pagination
            stmt = stmt.offset(offset).limit(limit)

            result = await self.db.execute(stmt)
            tasks = result.scalars().all()

            return list(tasks)

        except Exception as e:
            logger.error(f"Error getting scheduled tasks: {str(e)}")
            return []

    async def mark_scheduled_task_running(self, task_id: str) -> Optional[TaskModelType]:
        """
        Mark a scheduled task as running (in_progress) and reset for clean re-execution.

        NOTE: No longer used by the built-in scheduler, which moved to the
        clone-per-fire model (each fire instantiates a fresh run instance instead
        of resetting and re-running the definition in place). Kept as a general
        storage primitive (e.g. for an external pull-execute integration); safe to
        remove with its tests if no such caller materializes.

        Resets execution state (status, result, error, progress, timestamps) so
        the task starts fresh, just like a new tasks.execute call. Also resets
        child tasks if the task has children.

        If the task is already in_progress, returns None to avoid double-execution.

        Args:
            task_id: Task ID to mark as running

        Returns:
            Updated task, or None if task not found or already running
        """
        try:
            task = await self.get_task_by_id(task_id)
            if not task:
                return None

            # Guard: skip if task is still running to avoid double-execution
            if task.status == "in_progress":
                logger.info(f"Task {task_id} is already in_progress, skipping")
                return None

            from datetime import timezone as tz

            now = datetime.now(tz.utc)

            task.status = "in_progress"
            task.started_at = now
            task.completed_at = None
            task.result = None  # Clear previous result for clean re-execution
            task.error = None
            task.progress = 0.0

            await self.db.commit()
            await self.db.refresh(task)

            # Reset child tasks for clean re-execution
            if getattr(task, "has_children", False):
                await self.reset_task_tree_for_reexecution(task_id)

            logger.info(f"Marked scheduled task {task_id} as running")
            return task

        except Exception as e:
            logger.error(f"Error marking scheduled task running: {str(e)}")
            await self.db.rollback()
            return None

    async def complete_scheduled_run(
        self,
        task_id: str,
        success: bool = True,
        error: Optional[str] = None,
        calculate_next_run: bool = True,
        count_run: bool = True,
    ) -> Optional[TaskModelType]:
        """
        Complete a scheduled task run and update schedule tracking.

        This method only manages schedule-related fields. It does NOT touch
        execution state (status, result, progress, started_at, completed_at)
        — the executor sets those, and they should remain as-is so users
        can see execution results between runs.

        Updates:
        - last_run_at to current time
        - run_count incremented by 1
        - next_run_at calculated for next execution (if calculate_next_run=True)
        - schedule_enabled to False if max_runs reached or schedule expired

        If the task is still in_progress when this is called with success=False,
        it means the task never reached the executor (pre-execution failure),
        so status and error are set here as a fallback.

        Args:
            task_id: Task ID that completed
            success: Whether the execution was successful
            error: Optional error message (if failed)
            calculate_next_run: If True, calculate and set next_run_at
            count_run: If True, increment run_count. Pass False when advancing the
                schedule for a fire that did NOT produce a run here (e.g. a
                duplicate dispatch already handled by another node) so run_count
                is not double-counted while next_run_at still moves forward.

        Returns:
            Updated task, or None if task not found
        """
        try:
            task = await self.get_task_by_id(task_id)
            if not task:
                return None

            from datetime import timezone as tz

            now = datetime.now(tz.utc)

            # Update schedule tracking
            task.last_run_at = now
            if count_run:
                task.run_count = (task.run_count or 0) + 1

            # Handle pre-execution failure: task never reached executor
            if not success and task.status == "in_progress":
                task.status = "failed"
                if error is not None:
                    task.error = error

            # Check if schedule should be disabled
            should_disable = False

            # Check max_runs limit
            if task.max_runs is not None and task.run_count >= task.max_runs:
                should_disable = True
                logger.info(f"Task {task_id} reached max_runs ({task.max_runs})")

            # Check schedule_end_at
            if task.schedule_end_at is not None and now >= task.schedule_end_at:
                should_disable = True
                logger.info(f"Task {task_id} passed schedule_end_at")

            # For 'once' type, disable after first run
            if task.schedule_type == "once":
                should_disable = True

            if should_disable:
                task.schedule_enabled = False
                task.next_run_at = None
            else:
                # Calculate next run time
                if calculate_next_run and task.schedule_type and task.schedule_expression:
                    from apflow.core.storage.sqlalchemy.schedule_calculator import (
                        ScheduleCalculator,
                    )

                    next_run = ScheduleCalculator.calculate_next_run(
                        task.schedule_type,
                        task.schedule_expression,
                        from_time=now,
                    )
                    task.next_run_at = next_run

            await self.db.commit()
            await self.db.refresh(task)

            logger.info(
                f"Completed scheduled run for task {task_id}: "
                f"run_count={task.run_count}, next_run_at={task.next_run_at}, "
                f"schedule_enabled={task.schedule_enabled}"
            )
            return task

        except Exception as e:
            logger.error(f"Error completing scheduled run: {str(e)}")
            await self.db.rollback()
            return None

    async def initialize_schedule(
        self,
        task_id: str,
        from_time: Optional[datetime] = None,
    ) -> Optional[TaskModelType]:
        """
        Initialize or recalculate next_run_at for a scheduled task.

        Call this after setting schedule_type/expression to calculate
        the first (or next) run time.

        Args:
            task_id: Task ID to initialize
            from_time: Reference time for calculation (default: now)

        Returns:
            Updated task with next_run_at set, or None if error
        """
        try:
            task = await self.get_task_by_id(task_id)
            if not task:
                return None

            if not task.schedule_type or not task.schedule_expression:
                logger.warning(f"Task {task_id} missing schedule_type or schedule_expression")
                return task

            from datetime import timezone as tz

            if from_time is None:
                from_time = datetime.now(tz.utc)

            # Check schedule_start_at boundary
            if task.schedule_start_at and from_time < task.schedule_start_at:
                from_time = task.schedule_start_at

            from apflow.core.storage.sqlalchemy.schedule_calculator import ScheduleCalculator

            next_run = ScheduleCalculator.calculate_next_run(
                task.schedule_type,
                task.schedule_expression,
                from_time=from_time,
            )

            task.next_run_at = next_run

            await self.db.commit()
            await self.db.refresh(task)

            logger.info(f"Initialized schedule for task {task_id}: next_run_at={next_run}")
            return task

        except Exception as e:
            logger.error(f"Error initializing schedule: {str(e)}")
            await self.db.rollback()
            return None

    async def _set_original_task_has_reference_to_true(
        self, original_task_id: str
    ) -> Optional[TaskModelType]:
        """Update original task's has_references flag to True"""
        if not original_task_id:
            return None

        original_task = await self.get_task_by_id(original_task_id)
        if (
            original_task
            and hasattr(original_task, "has_references")
            and not getattr(original_task, "has_references", False)
        ):
            setattr(original_task, "has_references", True)
            return original_task

        return None

    async def save_task_tree(self, root_node: "TaskTreeNode") -> bool:
        """Save task tree structure to database recursively"""
        try:
            changed_tasks: List[TaskModelType] = []

            # Recursively save children with proper parent_id
            child_changed_tasks = await self._save_task_tree_recursive(root_node)
            changed_tasks.extend(child_changed_tasks)

            self.add_tasks_in_db(changed_tasks)
            await self.db.commit()
            await self.refresh_tasks_in_db(changed_tasks)

            logger.info(f"Saved task tree: root task {root_node.task.id}")
            return True

        except Exception as e:
            logger.error(f"Error saving task tree to database: {e}")
            # Must be awaited: self.db is an async session proxy, so a bare
            # rollback() returns an un-awaited coroutine and leaves the session
            # stuck in a pending-rollback state for the next operation.
            await self.db.rollback()
            return False

    async def _save_task_tree_recursive(self, parent_node: "TaskTreeNode") -> List[TaskModelType]:
        """Recursively save children tasks with proper parent_id"""
        changed_tasks: List[TaskModelType] = []

        parent_task = parent_node.task
        changed_tasks.append(parent_task)

        original_task = await self._set_original_task_has_reference_to_true(
            parent_task.original_task_id
        )
        if original_task:
            changed_tasks.append(original_task)

        for child_node in parent_node.children:
            child_task = child_node.task
            # Set parent_id to the parent task's actual ID
            child_task.parent_id = parent_node.task.id
            changed_tasks.append(child_task)

            original_task = await self._set_original_task_has_reference_to_true(
                child_task.original_task_id
            )
            if original_task:
                changed_tasks.append(original_task)

            # Recursively save grandchildren
            if child_node.children:
                grandchild_changed_tasks = await self._save_task_tree_recursive(child_node)
                changed_tasks.extend(grandchild_changed_tasks)

        return changed_tasks

    async def get_all_children_recursive(self, task_id: str) -> List[TaskModelType]:
        """
        Get all children tasks recursively (including grandchildren, etc.)

        Args:
            task_id: Parent task ID

        Returns:
            List of all child TaskModel instances (or custom TaskModel subclass) recursively
        """
        all_children = []

        async def collect_children(parent_id: str):
            children = await self.get_child_tasks_by_parent_id(parent_id)
            for child in children:
                all_children.append(child)
                # Recursively collect grandchildren
                await collect_children(child.id)

        await collect_children(task_id)
        return all_children

    async def delete_task(self, task_id: str) -> bool:
        """
        Physically delete a task from the database

        Args:
            task_id: Task ID to delete

        Returns:
            True if successful, False if task not found
        """
        try:
            task = await self.get_task_by_id(task_id)
            if not task:
                return False

                # For async session, use delete statement
            stmt = delete(self.task_model_class).where(self.task_model_class.id == task_id)
            await self.db.execute(stmt)
            await self.db.commit()

            logger.debug(f"Physically deleted task {task_id}")
            return True

        except Exception as e:
            logger.error(f"Error deleting task {task_id}: {str(e)}")
            await self.db.rollback()
            return False

    async def get_task_tree_for_api(self, root_task: TaskModelType) -> "TaskTreeNode":
        """
        Get the task tree for API/CLI queries, recursively resolving link origin_type for all nodes.
        If any task (root or child) is a link, use its original task's data to build the tree, but keep allowlisted fields from the link node itself.
        The allowlist is configurable via APFLOW_TASK_LINK_KEEP_FIELDS environment variable (comma-separated).
        Args:
            root_task: The root task node to start from.
        Returns:
            TaskTreeNode for the appropriate root task, with all children resolved.
        """
        import os
        from apflow.core.types import TaskTreeNode

        # Allowlist of fields to keep from the link node itself
        default_keep_fields = [
            "id",
            "parent_id",
            "user_id",
            "task_tree_id",
            "origin_type",
            "created_at",
            "updated_at",
        ]
        keep_fields = os.getenv("APFLOW_TASK_LINK_KEEP_FIELDS")
        if keep_fields:
            keep_fields = [f.strip() for f in keep_fields.split(",") if f.strip()]
            task_fields = set(field.name for field in root_task.__table__.columns)
            # Validate keep_fields against actual task model fields
            for field in keep_fields:
                if field not in task_fields:
                    raise ValueError(
                        f"Invalid field '{field}' in APFLOW_TASK_LINK_KEEP_FIELDS; not a valid TaskModel field."
                    )
        else:
            keep_fields = default_keep_fields

        async def resolve_task(task: TaskModelType) -> Optional[TaskModelType]:
            # Recursively resolve link
            while getattr(task, "origin_type", None) == TaskOriginType.link and getattr(
                task, "original_task_id", None
            ):
                original_task = await self.get_task_by_id(task.original_task_id)
                if not original_task:
                    logger.error(f"Original task not found for id {task.original_task_id}")
                    raise ValidationError(f"Original task not found for id {task.original_task_id}")
                task = original_task
            return task

        def merge_task(link_task: TaskModelType, original_task: TaskModelType) -> TaskModelType:
            # Use TaskModel.copy for safe copying and merging
            override = {
                field: getattr(link_task, field)
                for field in keep_fields
                if hasattr(link_task, field)
            }
            return original_task.copy(override=override)

        async def build_tree(task: TaskModelType) -> TaskTreeNode:
            if getattr(task, "origin_type", None) == TaskOriginType.link and getattr(
                task, "original_task_id", None
            ):
                original_task = await resolve_task(task)
                merged_task = merge_task(task, original_task)
            else:
                merged_task = task
            node = TaskTreeNode(task=merged_task)
            child_tasks = await self.get_child_tasks_by_parent_id(merged_task.id)
            for child in child_tasks:
                child_node = await build_tree(child)
                node.add_child(child_node)
            return node

        return await build_tree(root_task)

    async def task_tree_id_exists(self, task_tree_id: str) -> bool:
        """Check if any task exists with the given task_tree_id"""
        stmt = (
            select(self.task_model_class)
            .filter(self.task_model_class.task_tree_id == task_tree_id)
            .limit(1)
        )
        result = await self.db.execute(stmt)
        task = result.scalar_one_or_none()
        return task is not None

    async def task_has_references(
        self, task_id: str, origin_type: Optional[TaskOriginType] = None
    ) -> bool:
        """
        Check if a task is referenced by other tasks.

        Args:
            task_id: The ID of the task to check.
            origin_type: Optional origin type to filter references.

        Returns:
            True if the task is referenced, False otherwise.

        Raises:
            ValueError: If the task does not exist.
        """
        task: Optional[TaskModelType] = await self.get_task_by_id(task_id)
        if task is None:
            raise ValueError(f"Task with id {task_id} not found")

        if not hasattr(task, "has_references"):
            return False

        if not getattr(task, "has_references", False):
            return False

        filters = [self.task_model_class.original_task_id == task_id]
        if origin_type is not None:
            filters.append(self.task_model_class.origin_type == origin_type)

        stmt = select(self.task_model_class).filter(*filters).limit(1)
        result = await self.db.execute(stmt)
        reference = result.scalar_one_or_none()

        if reference is None:
            if origin_type is None:
                # only reset has_references if no origin_type filter (all references)
                logger.info(
                    f"Resetting has_references to False for task {task_id} as no references found"
                )
                await self.update_task(task_id, has_references=False)
            return False

        return True

    async def list_scheduled_runs(
        self,
        task_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> List[TaskModelType]:
        """
        List the frozen run-history snapshots of a scheduled task, newest first.

        Returns the root row of each scheduled run (origin_type=scheduled_run)
        that points back to the given definition via original_task_id. Child
        rows of a run point to their own ancestors, so filtering on
        original_task_id uniquely selects run roots. Each returned row carries
        that fire's result, status, error, and token_usage.

        Args:
            task_id: The scheduled definition ID.
            limit: Maximum number of runs to return.
            offset: Number of runs to skip (for pagination).

        Returns:
            List of snapshot root tasks ordered by created_at descending.
        """
        stmt = (
            select(self.task_model_class)
            .filter(
                self.task_model_class.original_task_id == task_id,
                self.task_model_class.origin_type == TaskOriginType.scheduled_run,
            )
            .order_by(self.task_model_class.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    def add_tasks_in_db(self, tasks: List[TaskModelType]) -> None:
        """add tasks from database to get latest state"""
        for task in tasks:
            self.db.add(task)

    async def refresh_tasks_in_db(self, tasks: List[TaskModelType]) -> None:
        """Refresh tasks from database to get latest state"""
        for task in tasks:
            await self.db.refresh(task)


__all__ = [
    "TaskRepository",
]
