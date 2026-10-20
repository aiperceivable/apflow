from apflow.core.storage.sqlalchemy.models import TaskModel
from apflow.core.types import TaskTreeNode
from typing import List, Union


def check_tasks_user_ownership(
    tasks: Union[TaskTreeNode, TaskModel, List[TaskModel]], user_id: str
) -> bool:
    """
    Check tasks belong to the specified user ID.

    Args:
        tasks: Single task model, task tree node, or list of tasks model instances to validate.
        user_id: The user ID to check against.

    Returns:
        bool: True if all tasks belong to the specified user, False otherwise.
    """
    # Optimization 1: Handle edge case of empty list/empty node
    if not tasks:
        return True  # Can be adjusted to False according to business requirements

    # Optimization 2: Fix type judgment logic (original logic only judged single node, not list)
    if isinstance(tasks, TaskTreeNode):
        task_list = tasks.to_list()
    elif isinstance(tasks, list):
        task_list = tasks
    elif isinstance(tasks, object):
        task_list = [tasks]
    else:
        # Optimization 3: Explicitly handle unexpected types to avoid implicit errors
        return False

    # Core validation logic (unchanged)
    for task in task_list:
        # Optimization 4: Add attribute existence check to avoid AttributeError
        if not hasattr(task, "user_id") or task.user_id != user_id:
            return False

    return True
