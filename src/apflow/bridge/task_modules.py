"""
Task management modules for apcore registration.

These duck-typed modules expose apflow's core task CRUD operations
so they appear as tools in MCP, skills in A2A, and commands in CLI.

Note: Repository methods are async (AsyncSession). All execute() methods
use await to call the repository correctly.
"""

import copy
from typing import Any


def _make_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy to prevent class-level mutation by apcore."""
    return copy.deepcopy(schema)


_TASK_CREATE_INPUT = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "description": "Task name"},
        "inputs": {"type": "object", "description": "Task input parameters"},
        "params": {"type": "object", "description": "Executor init parameters"},
        "parent_id": {"type": "string", "description": "Parent task ID"},
        "priority": {
            "type": "integer",
            "minimum": 0,
            "maximum": 3,
            "default": 2,
            "description": "Priority: 0=urgent, 1=high, 2=normal, 3=low",
        },
        "dependencies": {"type": "array", "items": {"type": "object"}},
        "token_budget": {"type": "integer", "minimum": 0},
        "cost_policy": {"type": "string"},
        "max_attempts": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "default": 3,
        },
    },
    "required": ["name"],
}

_TASK_CREATE_OUTPUT = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "status": {"type": "string"},
        "created_at": {"type": "string"},
    },
    "required": ["id"],
}

_TASK_ID_INPUT = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "minLength": 1, "description": "Task ID"},
    },
    "required": ["task_id"],
}

_TASK_LIST_INPUT = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["pending", "in_progress", "completed", "failed", "cancelled"],
        },
        "user_id": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 50},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
    },
}


class TaskCreateModule:
    """Create a new task in the apflow task engine."""

    description = "Create a new task in the apflow task engine."

    def __init__(self, task_creator: Any, task_repository: Any) -> None:
        self._creator = task_creator
        self._repo = task_repository
        self.input_schema = _make_schema(_TASK_CREATE_INPUT)
        self.output_schema = _make_schema(_TASK_CREATE_OUTPUT)

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        name = inputs.get("name", "")
        if not name:
            raise ValueError("Task name must be non-empty")

        task_data: dict[str, Any] = {"name": name}
        for field in [
            "inputs",
            "params",
            "parent_id",
            "priority",
            "dependencies",
            "token_budget",
            "cost_policy",
            "max_attempts",
        ]:
            if field in inputs and inputs[field] is not None:
                task_data[field] = inputs[field]

        tasks = await self._creator.create_task_trees_from_array([task_data])
        root = tasks[0] if tasks else None
        if root is None:
            raise RuntimeError("Task creation returned no tasks")

        return {
            "id": root.id,
            "name": root.name,
            "status": root.status,
            "created_at": root.created_at.isoformat() if root.created_at else None,
        }


class TaskExecuteModule:
    """Execute an existing task in the apflow task engine."""

    description = "Execute an existing task in the apflow task engine."

    def __init__(self, task_manager: Any) -> None:
        self._manager = task_manager
        self.input_schema = _make_schema(_TASK_ID_INPUT)
        self.output_schema = _make_schema(
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "status": {"type": "string"},
                    "result": {"type": "object"},
                    "token_usage": {"type": "object"},
                },
            }
        )

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        result = await self._manager.execute_task(task_id)
        return result


class TaskListModule:
    """List tasks from the apflow task engine with optional filtering."""

    description = "List tasks from the apflow task engine with optional filtering."

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository
        self.input_schema = _make_schema(_TASK_LIST_INPUT)
        self.output_schema = _make_schema(
            {
                "type": "object",
                "properties": {
                    "tasks": {"type": "array", "items": {"type": "object"}},
                    "total": {"type": "integer"},
                },
            }
        )

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        limit = max(1, min(1000, inputs.get("limit", 50)))
        offset = max(0, inputs.get("offset", 0))

        # Use query_tasks which is the actual async repository method
        tasks = await self._repo.query_tasks(
            user_id=inputs.get("user_id"),
            status=inputs.get("status"),
            limit=limit,
            offset=offset,
        )

        return {
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "status": t.status,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
                for t in tasks
            ],
            "total": len(tasks),
        }


class TaskGetModule:
    """Get detailed information about a specific task."""

    description = "Get detailed information about a specific task."

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository
        self.input_schema = _make_schema(_TASK_ID_INPUT)
        self.output_schema = _make_schema({"type": "object"})

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        task = await self._repo.get_task_by_id(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")

        return task.to_dict()


class TaskDeleteModule:
    """Delete a task from the apflow task engine."""

    description = "Delete a task from the apflow task engine."

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository
        self.input_schema = _make_schema(_TASK_ID_INPUT)
        self.output_schema = _make_schema(
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "deleted": {"type": "boolean"},
                },
            }
        )

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        task = await self._repo.get_task_by_id(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")

        await self._repo.delete_task(task_id)
        return {"task_id": task_id, "deleted": True}


_TASK_CREATE_TREE_INPUT = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "minItems": 1,
            "description": "Array of task definitions. Use parent_id to build tree, dependencies for DAG ordering.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Optional task ID (auto-generated if omitted)"},
                    "name": {"type": "string", "minLength": 1, "description": "Task name (required)"},
                    "parent_id": {"type": "string", "description": "Parent task ID for tree structure"},
                    "priority": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 3,
                        "default": 2,
                        "description": "0=urgent, 1=high, 2=normal, 3=low",
                    },
                    "inputs": {"type": "object", "description": "Task input parameters"},
                    "params": {"type": "object", "description": "Executor init parameters"},
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Task dependencies for DAG ordering [{id: 'task_id', required: true}]",
                    },
                    "token_budget": {"type": "integer", "minimum": 0},
                    "cost_policy": {"type": "string"},
                    "max_attempts": {"type": "integer", "minimum": 1, "maximum": 100, "default": 3},
                },
                "required": ["name"],
            },
        },
    },
    "required": ["tasks"],
}

_TASK_CREATE_TREE_OUTPUT = {
    "type": "object",
    "properties": {
        "root_task_id": {"type": "string", "description": "ID of the first root task"},
        "task_count": {"type": "integer", "description": "Total tasks created"},
        "task_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "All created task IDs",
        },
    },
}


class TaskCreateTreeModule:
    """Create a complete task tree from an array of task definitions in one call."""

    description = (
        "Create a multi-step task workflow from an array of task definitions. "
        "Use parent_id for tree structure and dependencies for execution ordering. "
        "Tasks without parent_id are root tasks. Multiple roots are allowed."
    )

    def __init__(self, task_creator: Any, task_repository: Any) -> None:
        self._creator = task_creator
        self._repo = task_repository
        self.input_schema = _make_schema(_TASK_CREATE_TREE_INPUT)
        self.output_schema = _make_schema(_TASK_CREATE_TREE_OUTPUT)

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        tasks = inputs.get("tasks", [])
        if not tasks:
            raise ValueError("tasks array must be non-empty")

        for t in tasks:
            if not t.get("name"):
                raise ValueError("Each task must have a non-empty 'name'")

        tree = await self._creator.create_task_tree_from_array(tasks)

        # Collect all task IDs from the tree
        task_ids: list[str] = []

        def _collect_ids(node: Any) -> None:
            task_ids.append(node.task.id)
            for child in node.children:
                _collect_ids(child)

        _collect_ids(tree)

        return {
            "root_task_id": tree.task.id,
            "task_count": len(task_ids),
            "task_ids": task_ids,
        }
