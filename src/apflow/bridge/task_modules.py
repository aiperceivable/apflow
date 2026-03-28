"""
Task management modules for apcore registration.

These duck-typed modules expose apflow's core task CRUD operations
so they appear as tools in MCP, skills in A2A, and commands in CLI.
"""

from typing import Any, Dict


class TaskCreateModule:
    """Create a new task in the apflow task engine."""

    description = "Create a new task in the apflow task engine."
    input_schema: Dict[str, Any] = {
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
    output_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "status": {"type": "string"},
            "created_at": {"type": "string"},
        },
        "required": ["id"],
    }

    def __init__(self, task_creator: Any, task_repository: Any) -> None:
        self._creator = task_creator
        self._repo = task_repository

    async def execute(self, inputs: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
        name = inputs.get("name", "")
        if not name:
            raise ValueError("Task name must be non-empty")

        task_data = {"name": name}
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
            "created_at": str(root.created_at) if root.created_at else None,
        }


class TaskExecuteModule:
    """Execute an existing task in the apflow task engine."""

    description = "Execute an existing task in the apflow task engine."
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1, "description": "Task ID to execute"},
        },
        "required": ["task_id"],
    }
    output_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "status": {"type": "string"},
            "result": {"type": "object"},
            "token_usage": {"type": "object"},
        },
    }

    def __init__(self, task_manager: Any) -> None:
        self._manager = task_manager

    async def execute(self, inputs: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        result = await self._manager.execute_task(task_id)
        return result


class TaskListModule:
    """List tasks from the apflow task engine with optional filtering."""

    description = "List tasks from the apflow task engine with optional filtering."
    input_schema: Dict[str, Any] = {
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
    output_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "tasks": {"type": "array", "items": {"type": "object"}},
            "total": {"type": "integer"},
        },
    }

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def execute(self, inputs: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
        limit = max(1, min(1000, inputs.get("limit", 50)))
        offset = max(0, inputs.get("offset", 0))
        filters: Dict[str, Any] = {}
        if "status" in inputs:
            filters["status"] = inputs["status"]
        if "user_id" in inputs:
            filters["user_id"] = inputs["user_id"]

        tasks = self._repo.list_tasks(limit=limit, offset=offset, **filters)
        total = self._repo.count_tasks(**filters)

        return {
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "status": t.status,
                    "created_at": str(t.created_at) if t.created_at else None,
                }
                for t in tasks
            ],
            "total": total,
        }


class TaskGetModule:
    """Get detailed information about a specific task."""

    description = "Get detailed information about a specific task."
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1, "description": "Task ID"},
        },
        "required": ["task_id"],
    }
    output_schema: Dict[str, Any] = {"type": "object"}

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def execute(self, inputs: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        task = self._repo.get_task_by_id(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")

        return task.to_dict()


class TaskDeleteModule:
    """Delete a task from the apflow task engine."""

    description = "Delete a task from the apflow task engine."
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "minLength": 1, "description": "Task ID to delete"},
        },
        "required": ["task_id"],
    }
    output_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "deleted": {"type": "boolean"},
        },
    }

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository

    async def execute(self, inputs: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        task = self._repo.get_task_by_id(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")

        self._repo.delete_task(task_id)
        return {"task_id": task_id, "deleted": True}
