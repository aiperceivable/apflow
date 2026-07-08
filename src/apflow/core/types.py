"""
Core type definitions for apflow

This module contains core data structures and types that are shared across
different layers (execution, storage, api) to avoid circular dependencies.

These types represent the domain model of task orchestration and are not
tied to any specific implementation layer.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Dict, Any, Union, Callable, Awaitable, Optional

if TYPE_CHECKING:
    from apflow.core.storage.sqlalchemy.models import TaskModelType


# ============================================================================
# Type Aliases
# ============================================================================

TaskPreHook = Callable[["TaskModelType"], Union[None, Awaitable[None]]]
"""
Type alias for pre-execution hook functions.

Pre-hooks are called before task execution, allowing modification of task.inputs.
They receive only the TaskModelType instance and can modify it in-place.

Example:
    async def my_pre_hook(task: TaskModelType) -> None:
        if task.inputs is None:
            task.inputs = {}
        task.inputs["timestamp"] = datetime.now().isoformat()
"""

TaskPostHook = Callable[["TaskModelType", Dict[str, Any], Any], Union[None, Awaitable[None]]]


@dataclass
class WebhookVerifyContext:
    """Context passed to webhook verify hook for custom authentication"""

    task_id: str
    signature: Optional[str]
    timestamp: Optional[str]
    client_ip: str


@dataclass
class WebhookVerifyResult:
    """Result returned from webhook verify hook"""

    valid: bool
    user_id: Optional[str] = None
    error: Optional[str] = None


WebhookVerifyHook = Callable[
    [WebhookVerifyContext], Union[WebhookVerifyResult, Awaitable[WebhookVerifyResult]]
]
"""
Type alias for webhook verification hook functions.

Webhook verify hooks are called during webhook trigger authentication to perform
custom verification. They receive a WebhookVerifyContext and return a
WebhookVerifyResult indicating whether the request is valid.

Args:
    context: WebhookVerifyContext with task_id, signature, timestamp, client_ip

Returns:
    WebhookVerifyResult with valid (bool), optional user_id, and optional error

Example:
    async def my_verify_hook(ctx: WebhookVerifyContext) -> WebhookVerifyResult:
        valid = verify_signature(ctx.signature, ctx.timestamp)
        return WebhookVerifyResult(valid=valid, user_id="user123")
"""


# ============================================================================
# Task Status Constants
# ============================================================================


class TaskStatus:
    """
    Task status constants

    These constants represent the possible states of a task during its lifecycle.
    Use these constants instead of magic strings to ensure type safety and consistency.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def is_terminal(cls, status: str) -> bool:
        """
        Check if a status is terminal (task cannot transition from this state)

        Args:
            status: Task status string

        Returns:
            True if status is terminal (completed, failed, or cancelled)
        """
        return status in (cls.COMPLETED, cls.FAILED, cls.CANCELLED)

    @classmethod
    def is_active(cls, status: str) -> bool:
        """
        Check if a status represents an active task

        Args:
            status: Task status string

        Returns:
            True if status is pending or in_progress
        """
        return status in (cls.PENDING, cls.IN_PROGRESS)


# ============================================================================
# Task Priority
# ============================================================================

# Task Priority Notes:
# - Priority is an integer value used for task scheduling
# - TaskManager uses ASC (ascending) order following industry standard:
#   Lower numbers = Higher priority (executes earlier)
# - Recommended values (following industry standard):
#   0 = urgent (highest priority, executes first)
#   1 = high
#   2 = normal
#   3 = low (lowest priority, executes last)
# - This follows Linux kernel (nice values), POSIX, and real-time system conventions
# - If priority is None or missing, default to 999 (lowest priority, executes last)


class TaskTreeNode:
    """
    Task tree node for hierarchical task management

    This class represents a node in a task tree structure. It's a core data structure
    used by both the execution layer (TaskManager) and storage layer (TaskRepository)
    for building and managing task hierarchies.

    Attributes:
        task: The TaskModelType instance associated with this node
        children: List of child TaskTreeNode instances

    Methods:
        add_child: Add a child node to this node
        calculate_progress: Calculate the overall progress of the task tree
        calculate_status: Calculate the overall status of the task tree
    """

    def __init__(self, task: "TaskModelType"):
        """
        Initialize a task tree node

        Args:
            task: The TaskModelType instance to associate with this node
        """
        self.task = task
        self.children: List["TaskTreeNode"] = []

    def add_child(self, child: "TaskTreeNode"):
        """
        Add a child node to this node

        Args:
            child: The TaskTreeNode to add as a child
        """
        self.children.append(child)

    def has_children(self) -> bool:
        """
        Check if the node has any children

        Returns:
            True if the node has one or more children, False otherwise
        """
        return len(self.children) > 0

    def calculate_progress(self) -> float:
        """
        Calculate progress of the task tree

        Returns:
            Average progress of all child tasks (0.0 to 1.0)
            If no children, returns the task's own progress
        """
        if not self.children:
            return float(self.task.progress) if self.task.progress else 0.0

        total_progress = 0.0
        for child in self.children:
            total_progress += child.calculate_progress()

        return total_progress / len(self.children)

    def calculate_status(self) -> str:
        """
        Calculate overall status of the task tree

        Returns:
            Status string: "completed", "failed", "in_progress", "cancelled", or "pending"
            - "completed": All children are completed
            - "failed": At least one child has failed
            - "in_progress": At least one child is in progress
            - "cancelled": At least one child was cancelled (and none failed/in progress)
            - "pending": Otherwise
        """
        if not self.children:
            return self.task.status

        statuses = [child.calculate_status() for child in self.children]

        if all(s == "completed" for s in statuses):
            return "completed"
        elif any(s == "failed" for s in statuses):
            return "failed"
        elif any(s == "in_progress" for s in statuses):
            return "in_progress"
        elif any(s == "cancelled" for s in statuses):
            return "cancelled"
        else:
            return "pending"

    def __iter__(self):
        """
        Generator to iterate over all nodes in the tree

        Yields:
            Each TaskTreeNode in the tree (pre-order traversal)
        """
        yield self
        for child in self.children:
            yield from child

    def to_list(self) -> List["TaskModelType"]:
        """
        Convert the task tree to a flat list of TaskModelType instances

        Returns:
            List of TaskModelType instances in the tree
        """
        tasks = [self.task]
        for child in self.children:
            tasks.extend(child.to_list())
        return tasks

    def to_mapping(self) -> Dict[str, "TaskModelType"]:
        """
        Convert the task tree to a mapping of task IDs to TaskModelType instances

        Returns:
            Dictionary mapping task IDs to TaskModelType instances
        """
        mapping = {self.task.id: self.task}
        for child in self.children:
            mapping.update(child.to_mapping())
        return mapping

    def copy(self, data: Optional[Dict[str, Any]] = None) -> "TaskTreeNode":
        """
        Create a deep copy of the task tree node and its children

        Returns:
            A new TaskTreeNode instance that is a deep copy of this node
        """
        new_task = self.task.copy(data)
        new_node = TaskTreeNode(new_task)
        for child in self.children:
            new_node.children.append(child.copy(data))
        return new_node

    def update(self, data: Dict[str, Any]) -> "TaskTreeNode":
        """
        Update the task model associated with this node

        Args:
            kwargs: Key-value pairs to update on the TaskModelType
        """
        self.task.update_from_dict(data)
        for child in self.children:
            child.update(data)

        return self

    def output(self) -> Dict[str, Any]:
        """
        Generate a nested dictionary representing the task tree structure

        Returns:
            A dictionary with task IDs as keys and their child structures as values
        """
        return {"task": self.task.output(), "children": [child.output() for child in self.children]}

    def output_list(self) -> List[Dict[str, Any]]:
        """
        Generate a flat list of task dictionaries from the task tree

        Returns:
            A list of dictionaries representing each task in the tree
        """
        tasks = [self.task.output()]
        for child in self.children:
            tasks.extend(child.output_list())
        return tasks


__all__ = [
    "TaskTreeNode",
    "TaskPreHook",
    "TaskPostHook",
    "TaskStatus",
    "WebhookVerifyContext",
    "WebhookVerifyResult",
    "WebhookVerifyHook",
]
