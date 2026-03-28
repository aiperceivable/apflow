"""
Task creation and task tree management

This module provides comprehensive functionality for creating tasks and task trees:
1. Create task trees from tasks array (JSON format)
2. Create tasks by linking to existing tasks (reference)
3. Create tasks by copying existing tasks (allows modifications)
4. Create tasks by taking archives of existing tasks (frozen, read-only)

External callers should provide tasks with resolved id and parent_id.
This module validates that dependencies exist in the array and hierarchy is correct.

"""

from typing import List, Dict, Any, Optional, Set, TypeVar
import uuid
import os
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from apflow.core.types import TaskTreeNode, TaskStatus
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.core.storage.sqlalchemy.models import TaskModelType, TaskOriginType
from apflow.logger import get_logger
from apflow.core.config.registry import get_task_model_class
from sqlalchemy_session_proxy import SqlalchemySessionProxy
from apflow.core.validator import check_tasks_user_ownership
from apflow.core.validator.dependency_validator import (
    detect_circular_dependencies,
    validate_dependent_task_inclusion,
)

logger = get_logger(__name__)

# Type variable for TaskModel subclasses
TaskCreatorType = TypeVar("TaskCreatorType", bound="TaskCreator")

DEFAULT_MAX_DEPTH = os.getenv("APFLOW_MAX_DEPTH", 100)
DEFAULT_MAX_DEPTH = int(DEFAULT_MAX_DEPTH) if DEFAULT_MAX_DEPTH else 100


class TaskCreator:
    """
    Task creation and task tree management

    This class provides comprehensive functionality for creating tasks and task trees:
    1. Create task trees from tasks array (JSON format)
    2. Create tasks by linking to existing tasks (reference)
    3. Create tasks by copying existing tasks (allows modifications)
    4. Create tasks by taking archives of existing tasks (frozen, read-only)

    External callers should provide tasks with resolved id and parent_id.
    This module validates that dependencies exist in the array and hierarchy is correct.

    Public methods:
        - create_task_tree_from_array(): Create task tree from tasks array
        - from_link(): Create task by linking to existing task (reference)
        - from_copy(): Create task by copying existing task (allows modifications)
        - from_archive(): Create task by taking archive of existing task (frozen)
        - from_mixed(): Create task tree with mixed origin types (copy + link)
    """

    def __init__(self, db: Session | AsyncSession):
        """
        Initialize TaskCreator

        Args:
            db: Database session (sync or async)
        """
        self.db = SqlalchemySessionProxy(db)
        self.task_model_class = get_task_model_class()
        self.task_repository = TaskRepository(db, task_model_class=self.task_model_class)

    def task_dicts_to_task_models(self, tasks: List[Dict[str, Any]]) -> List[TaskModelType]:
        """
        Convert list of task dicts to list of TaskModelType instances

        Args:
            dicts: List of task dictionaries

        Returns:
            List of TaskModelType instances
        """
        if not tasks:
            return []

        # Step 1: Validate and collect names/ids, and build name->id mapping
        name_id_mapping: Dict[str, str] = {}
        id_set: Set[str] = set()
        name_set: Set[str] = set()
        # task id: task dict mapping for easy lookup
        tasks_mapping: Dict[str, Dict[str, Any]] = {}

        # First pass: assign id if missing, check uniqueness
        for idx, task in enumerate(tasks):
            name = task.get("name")
            if not name:
                raise ValueError(f"Task at index {idx} must have a 'name' field")
            if name in name_set:
                raise ValueError(f"Duplicate task name '{name}' at index {idx}")
            name_set.add(name)
            provided_id = task.get("id")
            if provided_id:
                if provided_id in id_set:
                    raise ValueError(f"Duplicate task id '{provided_id}' at index {idx}")
                id_set.add(provided_id)
                name_id_mapping[name] = provided_id
            else:
                # Generate a new UUID for this task
                new_id = str(uuid.uuid4())
                task["id"] = new_id
                id_set.add(new_id)
                name_id_mapping[name] = new_id

            tasks_mapping[task["id"]] = task

        # Step 2: Map all references (parent_id, dependencies) from name to id
        for idx, task in enumerate(tasks):
            # Map parent_id if it's a name
            parent_id = task.get("parent_id")
            if parent_id and parent_id not in id_set and parent_id in name_id_mapping:
                parent_id = name_id_mapping[parent_id]
                task["parent_id"] = parent_id
                tasks_mapping[parent_id]["has_children"] = True
            elif parent_id and parent_id in id_set:
                tasks_mapping[parent_id]["has_children"] = True
            elif parent_id:
                raise ValueError(
                    f"Task at index {idx} has parent_id '{parent_id}' which is not in the task array."
                )

            # Map dependencies
            dependencies = task.get("dependencies")
            if dependencies:
                new_deps = []
                dep_ids_seen: Set[str] = set()
                for dep in dependencies:
                    # Support both dict and string formats for dependencies
                    if isinstance(dep, dict):
                        dep_id = dep.get("id") or dep.get("name")
                        if dep_id in id_set:
                            dep = {**dep, "id": dep_id}
                            dep.pop("name", None)
                        elif dep_id in name_id_mapping:
                            dep_id = name_id_mapping[dep_id]
                            dep = {**dep, "id": dep_id}
                            dep.pop("name", None)
                        else:
                            raise ValueError(
                                f"Task at index {idx} has dependency reference '{dep_id}' which is not a valid id or name in the task array."
                            )
                    else:
                        dep_id = dep
                        if dep_id in id_set:
                            dep_id = dep_id
                        elif dep_id in name_id_mapping:
                            dep_id = name_id_mapping[dep_id]
                        else:
                            raise ValueError(
                                f"Task at index {idx} has dependency reference '{dep_id}' which is not a valid id or name in the task array."
                            )
                        dep = dep_id
                    # Avoid duplicate dependencies
                    if dep_id and dep_id not in dep_ids_seen:
                        dep_ids_seen.add(dep_id)
                        new_deps.append(dep)
                task["dependencies"] = new_deps

        # Step 3: Assign task_tree_id for each root task and propagate to children
        root_tasks = [task for task in tasks if task.get("parent_id") is None]
        tree_ids: Set[str] = set()
        for root_task in root_tasks:
            task_tree_id = root_task.get("task_tree_id") or root_task.get("id")
            self._update_task_tree_id_for_task_dics(tasks, root_task, task_tree_id)
            if task_tree_id in tree_ids:
                raise ValueError("Different root tasks cannot share the same task_tree_id")
            tree_ids.add(task_tree_id)

        all_ids = {task["id"] for task in tasks}

        # Step 4: Detect circular dependencies and validate dependent task inclusion (reuse existing helpers)
        # All references are now ids, so pass only id sets
        detect_circular_dependencies(tasks=tasks)
        validate_dependent_task_inclusion(tasks)

        # Step 5: Validate all references (parent_id, dependencies) are valid ids
        for idx, task in enumerate(tasks):
            parent_id = task.get("parent_id")
            if parent_id and parent_id not in all_ids:
                raise ValueError(
                    f"Task at index {idx} has parent_id '{parent_id}' which is not a valid id in the task array."
                )
            dependencies = task.get("dependencies")
            if dependencies:
                for dep in dependencies:
                    if isinstance(dep, dict):
                        dep_id = dep.get("id")
                        if dep_id and dep_id not in all_ids:
                            raise ValueError(
                                f"Task at index {idx} has dependency id '{dep_id}' which is not in the task array."
                            )
                    else:
                        if dep not in all_ids:
                            raise ValueError(
                                f"Task at index {idx} has dependency '{dep}' which is not in the task array."
                            )

        tasks_models: List[TaskModelType] = []
        for task_data in tasks:
            task = self.task_repository.build_task(**task_data)  # type: ignore
            tasks_models.append(task)
        return tasks_models

    async def create_task_tree_from_array(self, tasks: List[Dict[str, Any]]) -> TaskTreeNode:
        """
        Create task tree(s) from a flat array of task dictionaries.

        Supports both single-root and multi-root task forests.
        For single-root, returns the root TaskTreeNode.
        For multi-root, returns the first root TaskTreeNode
        (use create_task_trees_from_array() for all roots).

        Args:
            tasks: List of task dictionaries. Tasks with parent_id=None are root tasks.
                   Multiple roots are allowed.

        Returns:
            TaskTreeNode: Root task node of the first task tree
        """
        if not tasks:
            raise ValueError("Tasks array cannot be empty")

        logger.info(f"Creating task tree from {len(tasks)} tasks")

        # Step 1: Validate at least one root task exists
        root_tasks = [task for task in tasks if task.get("parent_id") is None]
        if len(root_tasks) == 0:
            raise ValueError(
                "No root task found (task with no parent_id). At least one task in the array must have parent_id=None or no parent_id field."
            )
        # Multi-root forests are allowed — no single-root constraint

        # Step 2: Convert dicts to tasks with validated references
        task_models = self.task_dicts_to_task_models(tasks)

        # Step 3: Check for existing task ids in DB (for provided ids that are not valid UUIDs)
        await self._check_tasks_existence(tasks)

        # Step 4: Create all tasks (parent_id and dependencies will be set after creation)
        self.task_repository.add_tasks_in_db(task_models)
        await self.db.commit()

        # Step 5: Build task tree structure
        root_models: List[TaskModelType] = [task for task in task_models if task.parent_id is None]
        if len(root_models) == 1:
            task_tree = self.build_task_tree_from_task_models(root_models[0], task_models)
            logger.info(f"Created task tree: root task {task_tree.task.id}")
            return task_tree
        else:
            task_trees = self.build_task_trees_from_task_models(task_models)
            logger.info(
                f"Created {len(task_trees)} task trees: "
                f"{', '.join(t.task.id for t in task_trees)}"
            )
            return task_trees[0]  # Backward compatible: return first tree

    async def create_task_trees_from_array(self, tasks: List[Dict[str, Any]]) -> List[TaskTreeNode]:
        """
        Create task trees from a list of task dicts.
        Supports both id and name as input, but always generates a unique UUID id for each task internally.
        All references (parent_id, dependencies) are mapped to id before creation, so downstream logic is unified.
        If an id is provided and is not a valid UUID, check for existence in the database to avoid conflicts.

        Args:
            tasks: List of task dictionaries

        Returns:
            List[TaskTreeNode]: List of root task nodes of the created task trees
        """

        if not tasks:
            raise ValueError("Tasks array cannot be empty")

        logger.info(f"Creating task trees from {len(tasks)} tasks")

        # Step 1: Convert dicts to tasks with validated references
        task_models = self.task_dicts_to_task_models(tasks)

        # Step 2: Check for existing task ids in DB (for provided ids that are not valid UUIDs)
        await self._check_tasks_existence(tasks)

        # Step 3: Create all tasks (parent_id and dependencies will be set after creation)
        self.task_repository.add_tasks_in_db(task_models)
        await self.db.commit()

        # Step 4: Build task trees structure

        task_trees = self.build_task_trees_from_task_models(task_models)
        logger.info(
            f"Created task trees, total root tasks: {', '.join([node.task.id for node in task_trees])}"
        )
        return task_trees

    async def _check_tasks_existence(self, tasks: List[Dict[str, Any]]) -> None:
        """
        Check if tasks with provided ids already exist in the database.
        Only checks tasks that have an id provided and is not a valid UUID.

        Args:
            tasks: List of task dictionaries
        """
        import re

        uuid_regex = re.compile(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
        )

        self.db.expire_all()

        root_tasks = [task for task in tasks if task.get("parent_id") is None]
        for tree_id in [root_task.get("task_tree_id") for root_task in root_tasks]:
            if tree_id and not uuid_regex.match(tree_id):
                existing_task = await self.task_repository.task_tree_id_exists(tree_id)
                if existing_task:
                    raise ValueError(
                        f"Task tree id already exists in database (tree_id: {tree_id})."
                    )

        for _, task_data in enumerate(tasks):
            provided_id = task_data.get("id")
            if provided_id and not uuid_regex.match(provided_id):
                existing_task = await self.task_repository.get_task_by_id(provided_id)
                if existing_task:
                    raise ValueError(f"Task id already exists in database (id: {provided_id}).")

    def _validate_dependencies(
        self,
        dependencies: List[Any],
        task_name: str,
        task_index: int,
        provided_ids: Set[str],
        id_to_index: Dict[str, int],
        task_names: Set[str],
        name_to_index: Dict[str, int],
    ) -> None:
        """
        Validate dependencies exist in the array and hierarchy is correct

        Args:
            dependencies: Dependencies list from task data
            task_name: Name of the task (for error messages)
            task_index: Index of the task in the array
            provided_ids: Set of all provided task IDs
            id_to_index: Map of id -> index in array
            task_names: Set of all task names (for name-based references)
            name_to_index: Map of name -> index in array

        Raises:
            ValueError: If dependencies are invalid
        """
        for dep in dependencies:
            if isinstance(dep, dict):
                # Support both "id" and "name" for dependency reference
                dep_ref = dep.get("id") or dep.get("name")
                if not dep_ref:
                    raise ValueError(
                        f"Task '{task_name}' dependency must have 'id' or 'name' field"
                    )

                # Validate dependency exists in the array (as id or name)
                dep_index = None
                if dep_ref in provided_ids:
                    dep_index = id_to_index.get(dep_ref)
                elif dep_ref in task_names:
                    dep_index = name_to_index.get(dep_ref)
                else:
                    raise ValueError(
                        f"Task '{task_name}' at index {task_index} has dependency reference '{dep_ref}' "
                        f"which is not in the tasks array (not found as id or name)"
                    )

                # Validate hierarchy: dependency should be at an earlier index (or same level)
                if dep_index is not None and dep_index >= task_index:
                    # This is allowed for same-level dependencies, but log a warning
                    logger.debug(
                        f"Task '{task_name}' at index {task_index} depends on task at index {dep_index}. "
                        f"This is allowed but may indicate a potential issue."
                    )
            else:
                # Simple string dependency (can be id or name)
                dep_ref = str(dep)
                if dep_ref not in provided_ids and dep_ref not in task_names:
                    raise ValueError(
                        f"Task '{task_name}' at index {task_index} has dependency '{dep_ref}' "
                        f"which is not in the tasks array (not found as id or name)"
                    )

    def build_task_trees_from_task_models(self, tasks: List[TaskModelType]) -> List[TaskTreeNode]:
        """
        Build task tree structure from flat list of tasks

        Args:
            tasks: List of TaskModelType instances

        Returns:
            List of TaskTreeNode representing the root nodes of the task tree
        """
        root_tasks: List[TaskModelType] = [task for task in tasks if task.parent_id is None]
        task_trees: List[TaskTreeNode] = []

        for root_task in root_tasks:
            task_node = self.build_task_tree_from_task_models(root_task, tasks)
            task_trees.append(task_node)

        return task_trees

    def build_task_tree_from_task_models(
        self, root_task: TaskModelType, all_tasks: List[TaskModelType]
    ) -> TaskTreeNode:
        """
        Build task tree structure from root task

        Args:
            root_task: Root task
            all_tasks: All created tasks

        Returns:
            TaskTreeNode: Root task node with children
        """
        # Create task node
        task_node = TaskTreeNode(task=root_task)

        # Find children (tasks with parent_id == root_task.id)
        children = [task for task in all_tasks if task.parent_id == root_task.id]

        # Recursively build children
        for child_task in children:
            child_node = self.build_task_tree_from_task_models(child_task, all_tasks)
            task_node.add_child(child_node)

        return task_node

    async def _find_dependent_tasks_for_identifiers(
        self, task_identifiers: Set[str], all_tasks: List[TaskModelType]
    ) -> List[TaskModelType]:
        """
        Find all tasks that depend on any of the specified task identifiers (including transitive dependencies).

        Args:
            task_identifiers: Set of task identifiers (id or name) to find dependents for
            all_tasks: All tasks in the same context

        Returns:
            List of tasks that depend on any of the specified identifiers (directly or transitively)
        """
        if not task_identifiers:
            return []

        # Find tasks that directly depend on any of these identifiers
        dependent_tasks = []
        for task in all_tasks:
            dependencies = getattr(task, "dependencies", None)
            if dependencies and isinstance(dependencies, list):
                for dep in dependencies:
                    if isinstance(dep, dict):
                        dep_id = dep.get("id")
                        if dep_id in task_identifiers:
                            dependent_tasks.append(task)
                            break
                    else:
                        # Simple string dependency
                        dep_ref = str(dep)
                        if dep_ref in task_identifiers:
                            dependent_tasks.append(task)
                            break

        # Recursively find tasks that depend on the dependent tasks
        all_dependent_tasks = set(dependent_tasks)
        processed_identifiers = set(task_identifiers)

        async def find_transitive_dependents(current_dependent_tasks: List[TaskModelType]):
            """Recursively find tasks that depend on current dependent tasks"""
            new_dependents = []
            for dep_task in current_dependent_tasks:
                dep_identifiers = {str(dep_task.id)}

                # Only process if not already processed
                if not dep_identifiers.intersection(processed_identifiers):
                    processed_identifiers.update(dep_identifiers)
                    # Find tasks that depend on this dependent task
                    for task in all_tasks:
                        if task in all_dependent_tasks:
                            continue  # Already in the set
                        task_deps = getattr(task, "dependencies", None)
                        if task_deps and isinstance(task_deps, list):
                            for dep in task_deps:
                                if isinstance(dep, dict):
                                    dep_id = dep.get("id")
                                    if dep_id in dep_identifiers:
                                        new_dependents.append(task)
                                        all_dependent_tasks.add(task)
                                        break
                                else:
                                    dep_ref = str(dep)
                                    if dep_ref in dep_identifiers:
                                        new_dependents.append(task)
                                        all_dependent_tasks.add(task)
                                        break

            if new_dependents:
                await find_transitive_dependents(new_dependents)

        await find_transitive_dependents(dependent_tasks)

        return list(all_dependent_tasks)

    async def _find_dependency_tasks_for_identifiers(
        self,
        task_identifiers: Set[str],
        all_tasks: List[TaskModelType],
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> List[TaskModelType]:
        """
        Find all tasks that the specified task identifiers depend on (upstream dependencies, including transitive).

        Args:
            task_identifiers: Set of task identifiers (id) to find dependencies for
            all_tasks: All tasks in the same context
            max_depth: Maximum recursion depth to prevent infinite loops (default: 100)

        Returns:
            List of tasks that the specified identifiers depend on (directly or transitively)
        """
        if not task_identifiers:
            return []

        # Build a map of task identifier to task for quick lookup
        tasks_by_identifier: Dict[str, TaskModelType] = {}
        for task in all_tasks:
            task_id = str(task.id)
            tasks_by_identifier[task_id] = task

        # Find tasks that directly depend on any of these identifiers (these are the upstream dependencies)
        dependency_tasks = []
        processed_identifiers = set(task_identifiers)
        identifiers_to_process = set(task_identifiers)

        async def find_transitive_dependencies(current_identifiers: Set[str], depth: int = 0):
            """Recursively find tasks that current identifiers depend on"""
            # Prevent infinite recursion
            if depth >= max_depth:
                logger.warning(
                    f"Maximum recursion depth ({max_depth}) reached in dependency resolution. "
                    f"Stopping to prevent infinite loop."
                )
                return

            new_dependency_identifiers = set()

            # For each task with an identifier in current_identifiers, find its dependencies
            for task in all_tasks:
                task_id = str(task.id)

                # Check if this task is in current_identifiers
                if task_id not in current_identifiers:
                    continue

                # Get dependencies for this task
                dependencies = getattr(task, "dependencies", None)
                if dependencies and isinstance(dependencies, list):
                    for dep in dependencies:
                        if isinstance(dep, dict):
                            dep_identifier = dep.get("id")
                        else:
                            dep_identifier = str(dep)

                        if dep_identifier and dep_identifier not in processed_identifiers:
                            # Found a new dependency identifier
                            processed_identifiers.add(dep_identifier)
                            new_dependency_identifiers.add(dep_identifier)

                            # If this dependency identifier corresponds to a task in all_tasks, add it
                            if dep_identifier in tasks_by_identifier:
                                dep_task = tasks_by_identifier[dep_identifier]
                                if dep_task not in dependency_tasks:
                                    dependency_tasks.append(dep_task)
                            else:
                                # Dependency not in all_tasks - try to find it in database
                                # This handles cases where dependencies are in different task trees
                                try:
                                    dep_task = await self.task_repository.get_task_by_id(
                                        dep_identifier
                                    )
                                    if dep_task and dep_task not in dependency_tasks:
                                        dependency_tasks.append(dep_task)
                                        # Add to tasks_by_identifier for future lookups
                                        tasks_by_identifier[dep_identifier] = dep_task
                                except Exception as e:
                                    logger.debug(
                                        f"Could not find dependency task {dep_identifier} in database: {e}. "
                                        f"It may be in a different task tree or not exist."
                                    )

            # Recursively process new dependency identifiers
            if new_dependency_identifiers:
                await find_transitive_dependencies(new_dependency_identifiers, depth + 1)

        await find_transitive_dependencies(identifiers_to_process, 0)

        return dependency_tasks

    async def _find_minimal_subtree(
        self, root_tree: TaskTreeNode, required_task_ids: Set[str]
    ) -> Optional[TaskTreeNode]:
        """
        Find minimal subtree that contains all required tasks.
        Returns None if not all required tasks are found in the tree.

        Args:
            root_tree: Root task tree to search in
            required_task_ids: Set of task IDs that must be included

        Returns:
            Minimal TaskTreeNode containing all required tasks, or None
        """

        def collect_task_ids(node: TaskTreeNode) -> Set[str]:
            """Collect all task IDs in the tree"""
            task_ids = {str(node.task.id)}
            for child in node.children:
                task_ids.update(collect_task_ids(child))
            return task_ids

        # Check if all required tasks are in the tree
        all_task_ids = collect_task_ids(root_tree)
        if not required_task_ids.issubset(all_task_ids):
            return None

        def build_minimal_subtree(node: TaskTreeNode) -> Optional[TaskTreeNode]:
            """Build minimal subtree containing required tasks"""
            # Collect task IDs in this subtree
            subtree_task_ids = collect_task_ids(node)

            # Check if this subtree contains any required tasks
            if not subtree_task_ids.intersection(required_task_ids):
                return None

            # If this node is required or has required descendants, include it
            new_node = TaskTreeNode(task=node.task)

            for child in node.children:
                child_subtree = build_minimal_subtree(child)
                if child_subtree:
                    new_node.add_child(child_subtree)

            return new_node

        return build_minimal_subtree(root_tree)

    def _tree_to_task_array(self, node: TaskTreeNode) -> List[Dict[str, Any]]:
        """
        Convert TaskTreeNode to flat task array compatible with tasks.create API.

        Uses TaskModelType's actual fields via get_task_model_class().
        Since tasks are not saved yet, uses name-based references instead of id.
        Ensures all names are unique.

        Args:
            node: Task tree node

        Returns:
            List of task dictionaries compatible with tasks.create format
        """
        # Get all column names from TaskModelType
        task_columns = set(self.task_model_class.__table__.columns.keys())

        tasks = []
        name_counter = {}  # Track name usage for uniqueness
        task_to_name = {}  # task object id -> unique name

        # First pass: assign unique names to all tasks
        def assign_names(current_node: TaskTreeNode):
            task = current_node.task
            original_name = task.name

            # Generate unique name if needed
            if original_name not in name_counter:
                name_counter[original_name] = 0
                unique_name = original_name
            else:
                name_counter[original_name] += 1
                unique_name = f"{original_name}_{name_counter[original_name]}"

            task_to_name[id(task)] = unique_name

            # Recursively process children
            for child in current_node.children:
                assign_names(child)

        assign_names(node)

        # Build mappings for dependencies conversion
        # Map original task.id and original_task_id to new generated id and name
        task_id_to_new_id: Dict[str, str] = {}  # original task.id -> new generated id
        task_id_to_name: Dict[str, str] = {}  # original task.id -> name (for name-based refs)

        # First pass: map all task.id to their names
        def build_id_mappings(current_node: TaskTreeNode):
            task = current_node.task
            task_id_to_name[str(task.id)] = task_to_name[id(task)]
            for child in current_node.children:
                build_id_mappings(child)

        build_id_mappings(node)

        # Second pass: map original_task_id to name (for name-based refs fallback)
        # This allows dependencies that reference original task IDs to be converted correctly
        def map_original_task_ids(current_node: TaskTreeNode):
            task = current_node.task
            if task.original_task_id:
                original_id = str(task.original_task_id)
                # Only map if not already in the mapping (avoid overwriting existing mappings)
                # This ensures that if original_task_id matches another task's id in the tree,
                # we use that task's name, not the current task's name
                if original_id not in task_id_to_name:
                    task_id_to_name[original_id] = task_to_name[id(task)]
            for child in current_node.children:
                map_original_task_ids(child)

        map_original_task_ids(node)

        # Third pass: pre-generate all new IDs for all tasks (needed for dependency conversion)
        def pre_generate_ids(current_node: TaskTreeNode):
            task = current_node.task
            task_id_str = str(task.id)
            # Check if this task.id has already been mapped (should not happen in a valid tree)
            if task_id_str in task_id_to_new_id:
                # This should not happen, but if it does, reuse the existing mapping
                # This ensures we don't create duplicate IDs
                return
            new_task_id = str(uuid.uuid4())
            # Map task.id to new id
            task_id_to_new_id[task_id_str] = new_task_id
            # Also map original_task_id to new id (if exists) for dependency conversion
            # This ensures dependencies that reference original_task_id can be converted correctly
            if task.original_task_id:
                original_id = str(task.original_task_id)
                # Only map if not already mapped (avoid overwriting if multiple tasks have same original_task_id)
                if original_id not in task_id_to_new_id:
                    task_id_to_new_id[original_id] = new_task_id

            # IMPORTANT: Dependencies in the copied task may reference original task IDs
            # We need to map those original IDs to the new IDs of the copied tasks
            # Iterate through all tasks in the tree to build a complete mapping
            dependencies = getattr(task, "dependencies", None)
            if dependencies:
                for dep in dependencies:
                    if isinstance(dep, dict) and "id" in dep:
                        dep_id = str(dep["id"])
                        # If this dependency ID is not yet mapped, we need to find which copied task
                        # corresponds to this original dependency ID
                        if dep_id not in task_id_to_new_id:
                            # Find the task in the tree that has this ID as its original_task_id
                            # or as its task.id (if it's a direct reference)
                            # This will be handled by iterating through all tasks
                            pass  # Will be handled in a separate pass
            for child in current_node.children:
                pre_generate_ids(child)

        pre_generate_ids(node)

        # Fourth pass: map dependency IDs that reference original task IDs
        # Dependencies in copied tasks may reference original task IDs from the original tree
        # We need to map those original IDs to the new IDs of the corresponding copied tasks
        # Strategy: For each dependency ID that's not yet mapped, find the task in the new tree
        # that corresponds to that original ID (by checking original_task_id or task.id)
        def find_task_by_original_id(
            current_node: TaskTreeNode, target_original_id: str
        ) -> Optional[TaskTreeNode]:
            """Find a task in the tree that corresponds to the given original task ID"""
            task = current_node.task
            # Check if this task's original_task_id matches, or if task.id matches (for direct references)
            if (task.original_task_id and str(task.original_task_id) == target_original_id) or str(
                task.id
            ) == target_original_id:
                return current_node
            # Recursively check children
            for child in current_node.children:
                result = find_task_by_original_id(child, target_original_id)
                if result:
                    return result
            return None

        def map_dependency_ids(current_node: TaskTreeNode):
            """Map all dependency IDs in the tree to new task IDs"""
            task = current_node.task
            dependencies = getattr(task, "dependencies", None)
            if dependencies:
                for dep in dependencies:
                    if isinstance(dep, dict) and "id" in dep:
                        dep_id = str(dep["id"])
                        # If this dependency ID is not yet mapped, find the corresponding task in the new tree
                        if dep_id not in task_id_to_new_id:
                            found_node = find_task_by_original_id(node, dep_id)
                            if found_node:
                                # Map the dependency ID to the new ID of the found task
                                found_new_id = task_id_to_new_id[str(found_node.task.id)]
                                task_id_to_new_id[dep_id] = found_new_id
                            # If not found, it will raise an error during conversion (which is correct)
            for child in current_node.children:
                map_dependency_ids(child)

        map_dependency_ids(node)

        # Fourth pass: build task array with id and name-based references
        def collect_tasks(
            current_node: TaskTreeNode,
            parent_name: Optional[str] = None,
            parent_id: Optional[str] = None,
        ):
            task = current_node.task
            unique_name = task_to_name[id(task)]

            # Build task dict using TaskModelType's actual fields
            task_dict: Dict[str, Any] = {}

            # Get pre-generated UUID for this task (for save=False, tasks.create needs complete data)
            new_task_id = task_id_to_new_id[str(task.id)]
            task_dict["id"] = new_task_id

            # Handle parent_id separately (before the loop, since we skip it in the loop)
            # Use parent id (since all tasks have id now)
            # parent_id parameter is the new generated id of the parent task
            if parent_id is not None:
                task_dict["parent_id"] = parent_id
            # else: don't set parent_id (root task) - this is correct

            # Get all TaskModelType fields and their values
            for column_name in task_columns:
                # Skip id (already set above), parent_id (handled separately above), created_at, updated_at, has_references (auto-generated or not needed for create)
                if column_name in ("id", "parent_id", "created_at", "updated_at", "has_references"):
                    continue

                # Get value from task
                value = getattr(task, column_name, None)

                # Handle special cases
                if column_name == "name":
                    # Use unique name
                    task_dict["name"] = unique_name
                elif column_name == "progress":
                    # Convert Numeric to float
                    task_dict["progress"] = float(value) if value is not None else 0.0
                elif column_name == "dependencies" and value is not None:
                    # Convert dependencies: replace original id references with new generated id
                    # Since all tasks have id now, dependencies must use id references
                    if isinstance(value, list):
                        converted_deps = []
                        for dep in value:
                            if isinstance(dep, dict):
                                dep_copy = dep.copy()
                                # Convert id to new generated id (required for id-based mode)
                                if "id" in dep_copy:
                                    dep_id = str(dep_copy["id"])
                                    if dep_id in task_id_to_new_id:
                                        # Use new generated id
                                        dep_copy["id"] = task_id_to_new_id[dep_id]
                                    else:
                                        # If not found, this is an error - dependency must be in the tree
                                        raise ValueError(
                                            f"Dependency id '{dep_id}' not found in task tree. "
                                            f"All dependencies must reference tasks within the copied tree."
                                        )
                                # If dependency has "name" but no "id", try to find it by name
                                elif "name" in dep_copy:
                                    dep_name = dep_copy["name"]
                                    # Find task with this name and use its new id
                                    found = False
                                    for orig_id, new_id in task_id_to_new_id.items():
                                        if task_id_to_name.get(orig_id) == dep_name:
                                            dep_copy["id"] = new_id
                                            del dep_copy["name"]
                                            found = True
                                            break
                                    if not found:
                                        raise ValueError(
                                            f"Dependency name '{dep_name}' not found in task tree. "
                                            f"All dependencies must reference tasks within the copied tree."
                                        )
                                converted_deps.append(dep_copy)
                            else:
                                # String or other format - try to convert
                                dep_str = str(dep)
                                if dep_str in task_id_to_new_id:
                                    converted_deps.append({"id": task_id_to_new_id[dep_str]})
                                else:
                                    # Try to find by name
                                    found = False
                                    for orig_id, new_id in task_id_to_new_id.items():
                                        if task_id_to_name.get(orig_id) == dep_str:
                                            converted_deps.append({"id": new_id})
                                            found = True
                                            break
                                    if not found:
                                        raise ValueError(
                                            f"Dependency '{dep_str}' not found in task tree. "
                                            f"All dependencies must reference tasks within the copied tree."
                                        )
                        task_dict["dependencies"] = converted_deps
                    else:
                        task_dict["dependencies"] = value
                elif value is not None:
                    # Include non-None values
                    task_dict[column_name] = value

            tasks.append(task_dict)

            # Recursively collect children
            for child in current_node.children:
                collect_tasks(child, unique_name, new_task_id)

        collect_tasks(node, None, None)  # Root task has no parent
        return tasks

    async def _get_original_task_for_link(self, task: TaskModelType) -> TaskModelType:
        """
        Recursively find the most original (non-link) task for linking.
        """
        current = task
        max_iterations = 10  # Prevent infinite loops
        iterations = 0
        while getattr(current, "origin_type", None) == TaskOriginType.link and getattr(
            current, "original_task_id", None
        ):
            orig_id = current.original_task_id
            # get original task object (sync or async)
            current = await self.task_repository.get_task_by_id(orig_id)
            iterations += 1
            if iterations >= max_iterations:
                raise RuntimeError(
                    "Max iterations reached while finding original task for link. Possible circular reference."
                )

        return current

    def _link_reset_fields(self) -> Dict[str, Any]:
        """
        Fields to reset when creating a linked task.
        """
        return {
            "origin_type": TaskOriginType.link,
            "result": None,
            "params": None,
            "inputs": None,
            "schemas": None,
        }

    async def from_link(
        self,
        _original_task: TaskModelType,
        _save: bool = True,
        _recursive: bool = True,
        _auto_include_deps: bool = True,
        _include_dependents: bool = False,
        **reset_kwargs,
    ) -> TaskTreeNode:
        """
        Create task(s) by linking to existing task(s) (reference)
        Only allow linking if the entire source task tree is completed.
        The status of the link will be set to the source task's status.

        Creates new task(s) that reference the original task. Each new task points to
        the corresponding original task via original_task_id field and has origin_type='link'.

        Args:
            _original_task: Original task to link to
            _save: If True, save to database. If False, return in-memory instance(s)
            _recursive: If True, link entire subtree; if False, link only original_task
            _auto_include_deps: If True, automatically include upstream dependency tasks.
                Upstream tasks will be linked to, and minimal subtree will be built to connect them.
            _include_dependents: If True, also include downstream dependent tasks (non-root only).
            **reset_kwargs: Optional fields to override (e.g., user_id="new_user")

        Returns:
            TaskTreeNode
        """
        # check entire task tree is completed
        task_tree = await self.task_repository.build_task_tree(_original_task)
        task_tree_status = task_tree.calculate_status()
        if task_tree_status != TaskStatus.COMPLETED:
            logger.error(
                f"Only a fully completed task tree can be linked, status:{task_tree_status}."
            )
            raise ValueError(
                "Only a fully completed task tree can be linked. There are unfinished tasks in the tree."
            )

        if "user_id" in reset_kwargs and not check_tasks_user_ownership(
            task_tree, reset_kwargs.get("user_id")
        ):
            raise ValueError("Deny linking to a different user's task.")

        reset_kwargs.update(self._link_reset_fields())

        if not _recursive:
            reset_kwargs["parent_id"] = None  # No parent for single linked task
            reset_kwargs["dependencies"] = None  # No dependencies for single linked task
            new_task = await self._clone_task(_original_task, reset_kwargs)
            logger.info(
                f"Created linked task '{new_task.id}' referencing most original task '{_original_task.id}'"
            )
            task_tree = TaskTreeNode(task=new_task)
            if _save:
                await self.task_repository.save_task_tree(task_tree)
            return task_tree

        # Build original subtree and augment with dependencies as needed
        original_tree = await self.task_repository.build_task_tree(_original_task)
        if _original_task.parent_id is not None:
            # Validate no external dependencies if not root
            await self._validate_no_external_dependencies(_original_task)
            original_tree = await self._augment_subtree_with_dependencies(
                original_tree,
                _auto_include_deps,
                _include_dependents,
                None,
            )

        # clone tree
        task_tree = await self._clone_task_tree(original_tree, reset_kwargs)
        if _save:
            await self.task_repository.save_task_tree(task_tree)

        return task_tree

    async def from_copy(
        self,
        _original_task: TaskModelType,
        _save: bool = True,
        _recursive: bool = True,
        _auto_include_deps: bool = True,
        _include_dependents: bool = False,
        **reset_kwargs,
    ) -> TaskTreeNode:
        """
        Create task(s) by copying from existing task(s) (allows modifications)

        Copies the original task and optionally its entire subtree. The copied tasks
        can be modified. If the original task is not a root task and _recursive=True,
        validates that it and its children don't depend on tasks outside the subtree,
        and automatically promotes the copied subtree to an independent tree.

        Args:
            _original_task: Original task to copy from
            _save: If True, return saved instances. If False, return task array
            _recursive: If True, copy entire subtree; if False, copy only original_task
            _auto_include_deps: If True, automatically include upstream dependency tasks.
                               Only used when _recursive=True. Default: True
            _include_dependents: If True, also include downstream dependent tasks
                                (tasks that depend on the copied task). Only used when
                                _recursive=True and original task is not root. Default: False

        Returns:
            TaskTreeNode
        """
        task_tree = await self.task_repository.build_task_tree(_original_task)
        reset_kwargs = dict(reset_kwargs)
        reset_kwargs["origin_type"] = TaskOriginType.copy

        if not _recursive:
            reset_kwargs["parent_id"] = None  # No parent for single linked task
            reset_kwargs["dependencies"] = None  # No dependencies for single linked task
            new_task = await self._clone_task(_original_task, reset_kwargs)
            logger.info(
                f"Created linked task '{new_task.id}' referencing most original task '{_original_task.id}'"
            )
            task_tree = TaskTreeNode(task=new_task)
            if _save:
                await self.task_repository.save_task_tree(task_tree)
            return task_tree

        # Build original subtree and augment with dependencies as needed
        original_tree = await self.task_repository.build_task_tree(_original_task)
        if _original_task.parent_id is not None:
            # Validate no external dependencies if not root
            await self._validate_no_external_dependencies(_original_task)
            original_tree = await self._augment_subtree_with_dependencies(
                original_tree,
                _auto_include_deps,
                _include_dependents,
                None,
            )

        # clone tree
        task_tree = await self._clone_task_tree(original_tree, reset_kwargs)
        if _save:
            await self.task_repository.save_task_tree(task_tree)

        return task_tree

    async def from_archive(
        self,
        _original_task: TaskModelType,
        _save: bool = True,
        _recursive: bool = True,
        _auto_include_deps: bool = True,
        _include_dependents: bool = False,
    ) -> TaskTreeNode:
        """
        Create frozen archive(s) from existing task(s) (read-only, immutable)

        Creates frozen archives of the original task and optionally its entire subtree.
        Snapshot tasks cannot be modified after creation. If the original task is not
        a root task and _recursive=True, validates no external dependencies and
        automatically promotes the archive subtree to an independent tree.

        Args:
            _original_task: Original task to archive

        Returns:
            TaskTreeNode
        """

        # check entire task tree is completed
        task_tree = await self.task_repository.build_task_tree(_original_task)
        if task_tree.calculate_status() != TaskStatus.COMPLETED:
            raise ValueError(
                "Only a fully completed task tree can be linked. There are unfinished tasks in the tree."
            )

        reset_kwargs = {
            "origin_type": TaskOriginType.archive,
        }

        if not _recursive:
            _original_task.update_from_dict(reset_kwargs)
            logger.info(f"Archive task '{_original_task.id}'")
            task_tree = TaskTreeNode(task=_original_task)
            if _save:
                self.db.add(_original_task)
                await self.db.commit()
                await self.db.refresh(_original_task)
            return task_tree

        # Build original subtree and augment with dependencies as needed
        original_tree = await self.task_repository.build_task_tree(_original_task)
        if _original_task.parent_id is not None:
            # Validate no external dependencies if not root
            await self._validate_no_external_dependencies(_original_task)
            original_tree = await self._augment_subtree_with_dependencies(
                original_tree,
                _auto_include_deps,
                _include_dependents,
                None,
            )

        task_tree = original_tree.update(reset_kwargs)
        if _save:
            task_list = task_tree.to_list()
            self.task_repository.add_tasks_in_db(task_list)
            await self.db.commit()
            await self.task_repository.refresh_tasks_in_db(task_list)

        return task_tree

    async def from_mixed(
        self,
        _original_task: TaskModelType,
        _save: bool = True,
        _recursive: bool = True,
        _link_task_ids: Optional[List[str]] = None,
        _auto_include_deps: bool = True,
        _include_dependents: bool = False,
        **reset_kwargs,
    ) -> TaskModelType | TaskTreeNode | List[Dict[str, Any]]:
        """
        Create task tree with mixed origin types (copy + link)

        some tasks link (reference original), others copy (allow modification).

        Args:
            _original_task: Original task
            _save: If True, return saved instances. If False, return task array
            _recursive: If True, apply mixed mode to entire subtree; if False, only original_task
            _link_task_ids: List of task IDs to link (reference). Tasks NOT in this list will be copied.
                           If None, all tasks will be copied (equivalent to from_copy)
            _auto_include_deps: If True, automatically include upstream dependency tasks for copied portions.
                Only applies to tasks being copied, not linked.
            _include_dependents: If True, also include downstream dependent tasks for copied portions (non-root only).
            **reset_kwargs: Fields to override for copied tasks

        Returns:
            TaskModelType if _recursive=False,
            TaskTreeNode if _recursive=True and _save=True,
            List[Dict[str, Any]] if _save=False
        """
        if not _recursive:
            # Single task - determine if should link or copy
            if _link_task_ids and str(_original_task.id) in [str(id) for id in _link_task_ids]:
                if "user_id" in reset_kwargs and not check_tasks_user_ownership(
                    _original_task, reset_kwargs.get("user_id")
                ):
                    raise ValueError("Deny linking to a different user's task.")

                reset_kwargs.update(self._link_reset_fields())
                reset_kwargs["origin_type"] = TaskOriginType.link
            else:
                reset_kwargs["origin_type"] = TaskOriginType.copy

            new_task = await self._clone_task(_original_task, reset_kwargs)
            logger.info(
                f"Created {reset_kwargs['origin_type']} task '{new_task.id}' referencing most original task '{_original_task.id}'"
            )
            task_tree = TaskTreeNode(task=new_task)
            if _save:
                await self.task_repository.save_task_tree(task_tree)
            return task_tree

        # Recursive mixed - validate and handle with dependency consideration
        # is_root = _original_task.parent_id is None
        # validate external dependencies if not auto-including dependencies
        if not _auto_include_deps:
            await self._validate_no_external_dependencies(_original_task)

        # Build original subtree
        original_tree = await self.task_repository.build_task_tree(_original_task)

        if (
            _link_task_ids
            and "user_id" in reset_kwargs
            and not check_tasks_user_ownership(original_tree, reset_kwargs.get("user_id"))
        ):
            raise ValueError("Deny linking to a different user's task.")

        # Separate tasks into copy and link sets
        link_set = set(str(id) for id in (_link_task_ids if _link_task_ids else []))

        # Augment subtree with dependencies for copied portions only
        # To do so, temporarily mark linked tasks in the subtree so they are not considered
        # The augmentation helper considers the entire subtree; we emulate copied-only behavior
        # by building a subtree that excludes linked nodes when evaluating identifiers.
        mixed_tree = await self._augment_subtree_with_dependencies(
            original_tree,
            _auto_include_deps,
            _include_dependents,
            link_set,
        )

        # Create mixed tree with selective linking
        tasks_origin_type: Dict[str, TaskOriginType] = {}
        for id in link_set:
            tasks_origin_type[id] = TaskOriginType.link

        reset_kwargs["origin_type"] = TaskOriginType.copy
        task_tree = await self._clone_task_tree(mixed_tree, reset_kwargs, tasks_origin_type)

        if _save:
            await self.task_repository.save_task_tree(task_tree)

        return task_tree

    # ==================== Helper Methods ====================

    async def _augment_subtree_with_dependencies(
        self,
        original_tree: TaskTreeNode,
        auto_include_deps: bool,
        include_dependents: bool,
        excluded_identifiers: Optional[Set[str]] = None,
    ) -> TaskTreeNode:
        """Build a minimal subtree including upstream dependencies and optional dependents.
        Returns the augmented subtree if dependencies expand the set; otherwise returns the original subtree.
        """
        is_root = original_tree.task.parent_id is None

        # Collect all tasks for context
        root_task = await self.task_repository.get_root_task(original_tree.task)
        all_tasks = await self.task_repository.get_all_tasks_in_tree(root_task)

        # Collect IDs in the original subtree
        def collect_subtree_ids(tree_node: TaskTreeNode) -> Set[str]:
            ids = {str(tree_node.task.id)}
            if tree_node.children:
                for child in tree_node.children:
                    ids.update(collect_subtree_ids(child))
            return ids

        subtree_ids = collect_subtree_ids(original_tree)
        required_ids = set(subtree_ids)

        # Collect identifiers for dependency lookup
        task_identifiers = set()
        excluded_identifiers = excluded_identifiers or set()
        for task in all_tasks:
            if str(task.id) in subtree_ids and str(task.id) not in excluded_identifiers:
                task_identifiers.add(str(task.id))

        # Upstream dependencies
        if auto_include_deps and task_identifiers:
            try:
                dependency_tasks = await self._find_dependency_tasks_for_identifiers(
                    task_identifiers, all_tasks
                )
                for dep_task in dependency_tasks:
                    required_ids.add(str(dep_task.id))
            except Exception as e:
                logger.warning(
                    f"Failed to auto-include upstream dependencies: {e}. Proceeding with original subtree only."
                )

        # Downstream dependents (non-root only)
        if include_dependents and not is_root and task_identifiers:
            try:
                dependent_tasks = await self._find_dependent_tasks_for_identifiers(
                    task_identifiers, all_tasks
                )
                for dep_task in dependent_tasks:
                    required_ids.add(str(dep_task.id))
            except Exception as e:
                logger.warning(
                    f"Failed to include downstream dependents: {e}. Proceeding without dependents."
                )

        # If expanded set, build minimal subtree
        if len(required_ids) > len(subtree_ids):
            root_tree = await self.task_repository.build_task_tree(root_task)
            minimal_tree = await self._find_minimal_subtree(root_tree, required_ids)
            if minimal_tree:
                return minimal_tree
            logger.warning(
                "Failed to build minimal subtree with all required tasks. Using original subtree only."
            )

        return original_tree

    async def _clone_task_tree_dependency_fix(
        self, new_task_tree: TaskTreeNode, id_mapping: Dict[str, Any]
    ):
        """
        Clone task tree with dependency fixing.
        Args:
            new_task_tree: Cloned task tree to fix dependencies in
            id_mapping: Mapping from old task IDs to new task IDs
        Returns:
            None (modifies new_task_tree in place)
        """

        async def _fix_dependencies_recursive(
            task_node: TaskTreeNode,
            id_mapping: Dict[str, Any],
        ):
            """Recursively fix dependencies in the cloned task node"""
            # Fix dependencies for current task
            dependencies = getattr(task_node.task, "dependencies", None)
            if dependencies:
                missing_indexes = []
                for dep in dependencies:
                    if isinstance(dep, dict):
                        if "id" in dep:
                            dep_id = str(dep["id"])
                            if dep_id in id_mapping:
                                # Update to new ID
                                dep["id"] = id_mapping[dep_id]
                            else:
                                missing_indexes.append(dependencies.index(dep))  # Track missing IDs
                    else:
                        dep_id = str(dep)
                        if dep_id in id_mapping:
                            # Update to new ID
                            dep_index = dependencies.index(dep)
                            dependencies[dep_index] = id_mapping[dep_id]
                        else:
                            missing_indexes.append(dependencies.index(dep))  # Track missing IDs

                # Remove dependencies that could not be mapped
                for index in sorted(missing_indexes, reverse=True):
                    del dependencies[index]

            # Recursively fix children
            for child_node in task_node.children:
                await _fix_dependencies_recursive(child_node, id_mapping)

        await _fix_dependencies_recursive(new_task_tree, id_mapping)

    async def _clone_task_tree(
        self,
        original_task_tree: TaskTreeNode,
        reset_kwargs: Dict[str, Any] = {},
        tasks_origin_type: Optional[Dict[str, TaskOriginType]] = None,
    ) -> TaskTreeNode:
        """
        Recursively clone task tree for entire subtree
        Core Two Steps: Pure Copy → Reset ID → Recursively Process Child Nodes

        Args:
            original_task_tree: Original task tree to clone
            reset_kwargs: Fields to override/reset in cloned tasks
            tasks_origin_type: Optional mapping {original_task_id: origin_type} for mixed-mode cloning.
                If provided, overrides 'origin_type' for specific tasks (e.g., link/copy/archive).

        Returns:
            Cloned TaskTreeNode with updated tasks

        """

        # old task id: new task id mapping for dependency fixing
        id_mapping: Dict[str, str] = {}

        # new task tree
        new_tree = original_task_tree.copy()

        async def _reset_task_recursive(
            task_node: TaskTreeNode,
            reset_kwargs: Dict[str, Any],
        ) -> TaskTreeNode:
            """Recursively clone a task node and its children"""
            # Clone the task with overrides
            old_task_id = str(task_node.task.id)
            if tasks_origin_type and old_task_id in tasks_origin_type:
                reset_kwargs["origin_type"] = tasks_origin_type[old_task_id]
            cloned_task = await self._clone_task(task_node.task, reset_kwargs, False)
            task_node.task = cloned_task
            # Update id mapping
            id_mapping[old_task_id] = str(cloned_task.id)
            # Recursively clone children
            for child in task_node.children:
                child.task.parent_id = task_node.task.id
                await _reset_task_recursive(child, reset_kwargs)

            return task_node

        new_tree = await _reset_task_recursive(new_tree, reset_kwargs)

        if new_tree.task.parent_id is not None:
            new_tree.task.parent_id = None  # Promote to independent tree

        # Fix dependencies in the new tree
        await self._clone_task_tree_dependency_fix(new_tree, id_mapping)

        return new_tree

    def _reset_task_fields(
        self,
        task: TaskModelType,
        reset_kwargs: Dict[str, Any],
    ) -> None:
        """
        Reset specified fields on a task to their reset_kwargs values

        Args:
            task: Task to reset fields on
            field_names: List of field names to reset
        """
        # This method is called after the task is created with reset_kwargs
        # The reset_kwargs are already applied during task creation via _extract_field_overrides
        # This is a placeholder for any post-creation field resets if needed

        for field_name, field_value in reset_kwargs.items():
            if hasattr(task, field_name):
                setattr(task, field_name, field_value)

    async def _clone_task(
        self,
        original_task: TaskModelType,
        reset_kwargs: Dict[str, Any],
        is_copy: bool = True,
    ) -> TaskModelType:
        """
        Clone tasks' field overrides from reset_kwargs

        Args:
            original_task: Original task
            reset_kwargs: Field overrides
            is_copy: If True, return a copy; if False, update original task

        Returns:
            TaskModelType
        """
        reset_kwargs["id"] = str(uuid.uuid4())
        reset_kwargs["has_references"] = False

        most_original_task = await self._get_original_task_for_link(original_task)
        reset_kwargs["original_task_id"] = most_original_task.id

        if is_copy:
            task = original_task.copy(reset_kwargs)
        else:
            task = original_task.update_from_dict(reset_kwargs)

        if reset_kwargs.get("origin_type") == TaskOriginType.link:
            task = task.convert_to_link()

        return task

    async def _validate_no_external_dependencies(self, task: TaskModelType) -> None:
        """
        Validate that task and its subtree have no dependencies outside the subtree

        Args:
            task: Root task of the subtree to validate

        Raises:
            ValueError: If any task in the subtree depends on tasks outside the subtree
        """
        # Collect all tasks in the subtree
        subtree_tasks = await self._collect_subtree_tasks(task.id)
        subtree_task_ids = {t.id for t in subtree_tasks}

        # Check each task's dependencies
        for subtree_task in subtree_tasks:
            if not subtree_task.dependencies:
                continue
            for dep in subtree_task.dependencies:
                dep_id = dep.get("id")
                if not dep_id:
                    continue
                # Check if dependency is outside the subtree
                if dep_id not in subtree_task_ids:
                    raise ValueError(
                        f"Task '{subtree_task.name}' (id: {subtree_task.id}) has dependency "
                        f"on task '{dep_id}' which is outside the subtree rooted at '{task.name}' "
                        f"(id: {task.id}). Cannot copy/archive a subtree with external dependencies."
                    )

        logger.debug(
            f"Validated task '{task.name}' (id: {task.id}) subtree has no external dependencies"
        )

    async def _promote_to_independent_tree(self, root_task: TaskModelType) -> None:
        """
        Promote a task subtree to an independent tree

        Args:
            root_task: Root task of the subtree to promote
        """
        # Set root's parent_id to None (making it a true root)
        root_task.parent_id = None

        # Set task_tree_id for root and all descendants
        all_tasks = await self._collect_subtree_tasks(root_task.id)
        new_tree_id = root_task.id

        for task in all_tasks:
            task.task_tree_id = new_tree_id

        # Commit changes
        await self.db.commit()
        for task in all_tasks:
            await self.db.refresh(task)

        logger.info(
            f"Promoted task '{root_task.name}' (id: {root_task.id}) to independent tree "
            f"with {len(all_tasks)} total tasks"
        )

    async def _collect_subtree_tasks(self, root_task_id: str) -> List[TaskModelType]:
        """
        Collect all tasks in a subtree rooted at the given task

        Args:
            root_task_id: ID of the root task

        Returns:
            List[TaskModelType]: All tasks in the subtree (including root)
        """
        # Refresh session state before query to ensure we see latest database state
        # This prevents blocking in sync sessions when there are uncommitted transactions
        self.db.expire_all()
        root_task = await self.task_repository.get_task_by_id(root_task_id)
        if not root_task:
            return []

        all_tasks = [root_task]

        async def collect_children(task_id: str):
            """Recursively collect all child tasks"""
            children = await self.task_repository.get_child_tasks_by_parent_id(task_id)
            for child in children:
                all_tasks.append(child)
                await collect_children(child.id)

        await collect_children(root_task_id)
        return all_tasks

    def _update_task_tree_id_for_task_dics(
        self,
        all_tasks: List[Dict],
        parent_task: Dict,
        task_tree_id: str,
    ) -> None:
        """
        Update task_tree_id for task dicts in the array

        Args:
            tasks: List of task dicts
        """

        parent_task["task_tree_id"] = task_tree_id

        # Find children (tasks with parent_id == parent_task['id'])
        children = [task for task in all_tasks if task.get("parent_id") == parent_task.get("id")]

        # Recursively build children
        for child_task in children:
            self._update_task_tree_id_for_task_dics(all_tasks, child_task, task_tree_id)


__all__ = [
    "TaskCreator",
]
