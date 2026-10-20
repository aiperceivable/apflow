"""
Dependency validation utilities for task updates

This module provides reusable functions for validating task dependencies,
including circular dependency detection and dependency reference validation.

All dependency validation logic is centralized here for maintainability.
"""

from typing import Dict, Any, List, Set, Optional, Union, Protocol
from apflow.core.storage.sqlalchemy.models import TaskModel
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.logger import get_logger

logger = get_logger(__name__)


class _DependencyRepository(Protocol):
    """Queries needed to validate references and inspect active dependents."""

    async def get_task_by_id(self, task_id: str, /) -> Optional[TaskModel]: ...

    async def get_root_task(self, task: TaskModel, /) -> TaskModel: ...

    async def get_all_tasks_in_tree(self, root_task: TaskModel, /) -> List[TaskModel]: ...


def detect_circular_dependencies(
    tasks: Union[List[Dict[str, Any]], List[TaskModel]],
    task_id: Optional[str] = None,
    new_dependencies: Optional[List[Any]] = None,
    detail: bool = False,
) -> None:
    """
    Detect circular dependencies in the task graph.

    If task_id and new_dependencies are provided, simulate updating that task's dependencies.
    If not, check the current graph for cycles.

    Args:
        tasks: All tasks in the same task tree.
        task_id: (Optional) ID of the task being updated.
        new_dependencies: (Optional) New dependencies list for the task being updated.
        detail: If True, provide detailed cycle path in error message.

    Raises:
        ValueError: If circular dependencies are detected.
    """
    dependency_graph = _build_dependency_graph(tasks, task_id, new_dependencies)
    if detail:
        _detect_circular_dependencies_detail(dependency_graph)
    else:
        _detect_circular_dependencies_fast(dependency_graph)


def _build_dependency_graph(
    tasks: Union[List[Dict[str, Any]], List[TaskModel]],
    task_id: Optional[str] = None,
    new_dependencies: Optional[List[Any]] = None,
) -> Dict[str, Set[str]]:
    """
    Build a dependency graph for all tasks.

    If task_id and new_dependencies are provided, simulate updating that task's dependencies.
    Otherwise, use the current dependencies for all tasks.

    Args:
        tasks: List of all tasks (dict or model).
        task_id: (Optional) ID of the task being updated.
        new_dependencies: (Optional) New dependencies for the task being updated.

    Returns:
        A dictionary mapping task IDs to sets of dependent task IDs.
    """
    dependency_graph: Dict[str, Set[str]] = {}
    # Initialize graph nodes
    for task in tasks:
        tid = task["id"] if isinstance(task, dict) else task.id
        dependency_graph[tid] = set()
    # Override dependencies for the updated task if needed
    if task_id is not None and new_dependencies is not None:
        for dep in new_dependencies:
            dep_id = dep.get("id") if isinstance(dep, dict) else dep
            if dep_id and dep_id in dependency_graph:
                dependency_graph[task_id].add(dep_id)
    # Add dependencies for all other tasks (or all tasks if not simulating an update)
    for task in tasks:
        tid = task["id"] if isinstance(task, dict) else task.id
        if task_id is not None and new_dependencies is not None and tid == task_id:
            continue  # Already handled above
        task_deps = (
            task.get("dependencies", [])
            if isinstance(task, dict)
            else getattr(task, "dependencies", []) or []
        )
        for dep in task_deps:
            dep_id = dep.get("id") if isinstance(dep, dict) else dep
            if dep_id and dep_id in dependency_graph:
                dependency_graph[tid].add(dep_id)
    return dependency_graph


def _detect_circular_dependencies_fast(dependency_graph: Dict[str, Set[str]]) -> None:
    """
    Fast cycle detection using DFS, only reports the first node involved in a cycle.

    Args:
        dependency_graph: The dependency graph to check.

    Raises:
        ValueError: If a cycle is detected.
    """
    visited: Set[str] = set()
    stack: Set[str] = set()

    def dfs(node: str):
        if node in stack:
            raise ValueError(
                f"Circular dependency detected involving task '{node}'. "
                f"Tasks cannot have circular dependencies as this would cause infinite loops."
            )
        if node in visited:
            return
        stack.add(node)
        for dep in dependency_graph[node]:
            dfs(dep)
        stack.remove(node)
        visited.add(node)

    for identifier in dependency_graph.keys():
        if identifier not in visited:
            dfs(identifier)


def _detect_circular_dependencies_detail(dependency_graph: Dict[str, Set[str]]) -> None:
    """
    Detailed cycle detection using DFS, reports the full cycle path.

    Args:
        dependency_graph: The dependency graph to check.

    Raises:
        ValueError: If a cycle is detected, with the full cycle path.
    """
    visited: Set[str] = set()

    def dfs(node: str, path: List[str]) -> Optional[List[str]]:
        if node in path:
            cycle_start = path.index(node)
            cycle = path[cycle_start:] + [node]
            return cycle
        if node in visited:
            return None
        visited.add(node)
        path.append(node)
        for dep in dependency_graph.get(node, set()):
            if dep not in dependency_graph:
                continue
            cycle = dfs(dep, path)
            if cycle:
                return cycle
        path.pop()
        return None

    for identifier in dependency_graph.keys():
        if identifier not in visited:
            cycle_path = dfs(identifier, [])
            if cycle_path:
                raise ValueError(
                    f"Circular dependency detected: {' -> '.join(cycle_path)}. "
                    f"Tasks cannot have circular dependencies as this would cause infinite loops."
                )


def validate_dependent_task_inclusion(tasks: List[Dict[str, Any]]) -> None:
    """
    Validate that all tasks which depend (directly or transitively) on any task in the provided tasks array
    are also included in the array.

    This function only checks for missing downstream dependents within the given tasks array.
    It does NOT check whether dependencies referenced by each task actually exist in the array.

    Args:
        tasks: List of task dictionaries (the full set of tasks to be created/imported)

    Raises:
        ValueError: If any task that depends on a task in the array is missing from the array.
    """
    # Collect all task identifiers in the current tree
    tree_identifiers: Set[str] = set()
    for task_data in tasks:
        provided_id = task_data.get("id")
        if provided_id:
            tree_identifiers.add(provided_id)

    # Find all tasks that depend on tasks in the tree (including transitive)
    all_dependent_tasks = _find_transitive_dependents(tree_identifiers, tasks)

    # Check if all dependent tasks are included in the tree
    included_identifiers = tree_identifiers.copy()
    missing_dependents = []

    for dep_task in all_dependent_tasks:
        dep_identifier = dep_task.get("id")
        if dep_identifier and dep_identifier not in included_identifiers:
            missing_dependents.append(dep_task)

    if missing_dependents:
        missing_ids = [task.get("id", "Unknown") for task in missing_dependents]
        raise ValueError(
            f"Missing dependent tasks: {missing_ids}. "
            f"All tasks that depend on tasks in the tree must be included. "
            f"These tasks depend on tasks in the tree but are not included in the tasks array."
        )


def _find_transitive_dependents(
    task_identifiers: Set[str], all_tasks: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Find all tasks that depend on any of the specified task identifiers (including transitive).

    Args:
        task_identifiers: Set of task identifiers (id or name) to find dependents for
        all_tasks: All tasks in the array

    Returns:
        List of tasks that depend on any of the specified task identifiers (directly or transitively)
    """
    # Track all dependent tasks found (to avoid duplicates)
    found_dependents: Set[int] = set()  # Track by index to avoid duplicates
    dependent_tasks: List[Dict[str, Any]] = []

    # Start with the initial set of task identifiers
    current_identifiers = task_identifiers.copy()
    processed_identifiers: Set[str] = set()

    # Recursively find all transitive dependents
    while current_identifiers:
        next_identifiers: Set[str] = set()

        for identifier in current_identifiers:
            if identifier in processed_identifiers:
                continue
            processed_identifiers.add(identifier)

            # Find direct dependents
            for index, task_data in enumerate(all_tasks):
                if index in found_dependents:
                    continue

                dependencies = task_data.get("dependencies")
                if not dependencies:
                    continue

                # Check if this task depends on the current identifier
                depends_on_identifier = False
                for dep in dependencies:
                    if isinstance(dep, dict):
                        dep_ref = dep.get("id")
                        if dep_ref == identifier:
                            depends_on_identifier = True
                            break
                    else:
                        dep_ref = str(dep)
                        if dep_ref == identifier:
                            depends_on_identifier = True
                            break

                if depends_on_identifier:
                    found_dependents.add(index)
                    dependent_tasks.append(task_data)

                    # Add this task's identifier to next iteration
                    task_identifier = task_data.get("id")
                    if task_identifier and task_identifier not in processed_identifiers:
                        next_identifiers.add(task_identifier)

        current_identifiers = next_identifiers

    return dependent_tasks


async def validate_dependency_references(
    task_id: str,
    new_dependencies: List[Any],
    task_repository: _DependencyRepository,
    user_id: Optional[str] = None,
    only_within_tree: Optional[bool] = False,
) -> None:
    """
    Validate that all dependency references exist and belong to the same user.
    If only_within_tree is True, dependencies must be in the same task tree.
    If False, dependencies can be any task with the same user_id.
    Args:
        task_id: ID of the task being updated
        new_dependencies: New dependencies list for the task
        task_repository: TaskRepository instance
        user_id: The user ID that all dependencies must match
        only_within_tree: If True, dependencies must be in the same task tree (default False)
    Raises:
        ValueError: If any dependency reference is not found or user_id does not match
    """
    # Get the task being updated
    task = await task_repository.get_task_by_id(task_id)
    if not task:
        raise ValueError(f"Task {task_id} not found")

    if only_within_tree:
        # Restrict dependencies to the same task tree
        root_task = await task_repository.get_root_task(task)
        all_tasks_in_tree = await task_repository.get_all_tasks_in_tree(root_task)
        task_ids_in_tree = {t.id for t in all_tasks_in_tree}
        for dep in new_dependencies:
            dep_id = None
            if isinstance(dep, dict):
                dep_id = dep.get("id")
            elif isinstance(dep, str):
                dep_id = dep
            if not dep_id:
                raise ValueError("Dependency must have 'id' field or be a string task ID")
            if dep_id not in task_ids_in_tree:
                raise ValueError(f"Dependency reference '{dep_id}' not found in task tree")
            dep_task = next((t for t in all_tasks_in_tree if t.id == dep_id), None)
            if dep_task is not None:
                dep_user_id = getattr(dep_task, "user_id", None)
                # Only check user_id if both are not None
                if user_id is not None and dep_user_id is not None and user_id != dep_user_id:
                    raise ValueError(f"Dependency '{dep_id}' does not belong to user '{user_id}'")
    else:
        # Allow dependencies to any task with the same user_id
        for dep in new_dependencies:
            dep_id = None
            if isinstance(dep, dict):
                dep_id = dep.get("id")
            elif isinstance(dep, str):
                dep_id = dep
            if not dep_id:
                raise ValueError("Dependency must have 'id' field or be a string task ID")
            dep_task = await task_repository.get_task_by_id(dep_id)
            if not dep_task:
                raise ValueError(f"Dependency reference '{dep_id}' not found for user '{user_id}'")
            dep_user_id = getattr(dep_task, "user_id", None)
            # Only check user_id if both are not None
            if user_id is not None and dep_user_id is not None and user_id != dep_user_id:
                raise ValueError(f"Dependency '{dep_id}' does not belong to user '{user_id}'")


async def check_dependent_tasks_executing(
    task_id: str, task_repository: _DependencyRepository
) -> List[str]:
    """
    Check if any tasks that depend on this task are currently executing.
    Args:
        task_id: ID of the task being updated
        task_repository: TaskRepository instance
    Returns:
        List of task IDs that depend on this task and are in_progress
    """
    # Find all tasks in the same tree
    task = await task_repository.get_task_by_id(task_id)
    if not task:
        raise ValueError(f"Task {task_id} not found")
    root_task = await task_repository.get_root_task(task)
    all_tasks_in_tree = await task_repository.get_all_tasks_in_tree(root_task)
    dependent_task_ids = []
    for t in all_tasks_in_tree:
        deps = t.dependencies or []
        for dep in deps:
            dep_id = dep.get("id") if isinstance(dep, dict) else dep
            if dep_id == task_id and getattr(t, "status", None) == "in_progress":
                dependent_task_ids.append(t.id)
    return dependent_task_ids


async def are_dependencies_satisfied(
    task: TaskModel, task_repository: TaskRepository, tasks_to_reexecute: set[str]
) -> bool:
    """
    Check if all dependencies for a task are satisfied

    Re-execution Logic:
    - A dependency is satisfied if the dependency task is `completed`
    - Even if a dependency is marked for re-execution, if it's already `completed`,
      its result is available and can satisfy dependent tasks
    - This allows dependent tasks to proceed while still allowing re-execution
      of dependencies if needed

    Args:
        task: Task to check dependencies for
        task_repository: TaskRepository instance for querying tasks
        tasks_to_reexecute: Set of task IDs marked for re-execution

    Returns:
        True if all dependencies are satisfied, False otherwise
    """
    task_dependencies = task.dependencies or []
    if not task_dependencies:
        logger.info(f"🔍 [DEBUG] No dependencies for task {task.id}, ready to execute")
        return True

    # Get all completed tasks by id in the same task tree using repository
    completed_tasks_by_id = await task_repository.get_completed_tasks_by_id(task)
    logger.info(f"🔍 [DEBUG] Available tasks for {task.id}: {list(completed_tasks_by_id.keys())}")

    # Check each dependency
    for dep in task_dependencies:
        if isinstance(dep, dict):
            dep_id = dep.get("id")  # This is the task id of the dependency
            dep_required = dep.get("required", True)

            logger.info(
                f"🔍 [DEBUG] Checking dependency {dep_id} (required: {dep_required}) for task {task.id}"
            )

            if dep_required and dep_id not in completed_tasks_by_id:
                logger.info(
                    f"❌ Task {task.id} dependency {dep_id} not satisfied (not found in tasks: {list(completed_tasks_by_id.keys())})"
                )
                return False
            elif dep_required and dep_id in completed_tasks_by_id:
                # Check if the dependency task is actually completed
                dep_task = completed_tasks_by_id[dep_id]
                dep_task_id = str(dep_task.id)
                # If dependency is marked for re-execution and is still in progress or pending, it's not satisfied yet
                # But if it's already completed, we can consider it satisfied (it will be re-executed but result is available)
                if dep_task_id in tasks_to_reexecute:
                    # Check current status from database to see if it's actually completed
                    # If it's completed, we can use the result even if marked for re-execution
                    if dep_task.status == "completed":
                        logger.info(
                            f"✅ Task {task.id} dependency {dep_id} satisfied (task {dep_task.id} completed, marked for re-execution but result available)"
                        )
                    else:
                        logger.info(
                            f"❌ Task {task.id} dependency {dep_id} is marked for re-execution and not completed yet (status: {dep_task.status})"
                        )
                        return False
                elif dep_task.status != "completed":
                    logger.info(
                        f"❌ Task {task.id} dependency {dep_id} found but not completed (status: {dep_task.status})"
                    )
                    return False
                else:
                    logger.info(
                        f"✅ Task {task.id} dependency {dep_id} satisfied (task {dep_task.id} completed)"
                    )
        elif isinstance(dep, str):
            # Simple string dependency (just the id) - backward compatibility
            dep_id = dep
            if dep_id not in completed_tasks_by_id:
                logger.info(f"❌ Task {task.id} dependency {dep_id} not satisfied")
                return False
            dep_task = completed_tasks_by_id[dep_id]
            dep_task_id = str(dep_task.id)
            # If dependency is marked for re-execution, check if it's actually completed
            if dep_task_id in tasks_to_reexecute:
                # If it's completed, we can use the result even if marked for re-execution
                if dep_task.status == "completed":
                    logger.info(
                        f"✅ Task {task.id} dependency {dep_id} satisfied (task {dep_task.id} completed, marked for re-execution but result available)"
                    )
                else:
                    logger.info(
                        f"❌ Task {task.id} dependency {dep_id} is marked for re-execution and not completed yet (status: {dep_task.status})"
                    )
                    return False
            elif dep_task.status != "completed":
                logger.info(
                    f"❌ Task {task.id} dependency {dep_id} found but not completed (status: {dep_task.status})"
                )
                return False
            else:
                logger.info(
                    f"✅ Task {task.id} dependency {dep_id} satisfied (task {dep_task.id} completed)"
                )

    logger.info(f"✅ All dependencies satisfied for task {task.id}")
    return True
