"""
Task management modules for apcore registration.

These duck-typed modules expose apflow's core task CRUD operations
so they appear as tools in MCP, skills in A2A, and commands in CLI.

Note: Repository methods are async (AsyncSession). All execute() methods
use await to call the repository correctly.
"""

import asyncio
import contextlib
import copy
from collections.abc import AsyncIterator
from typing import Any

from apcore import Change, ModuleAnnotations, PreviewResult


def _make_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy to prevent class-level mutation by apcore."""
    return copy.deepcopy(schema)


def _coerce_int(
    value: Any,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Coerce an external input value to a clamped integer.

    Task-module inputs arrive from external agents (MCP/A2A) and are NOT
    schema-validated before ``execute()`` runs (apcore-mcp ``validate_inputs``
    defaults to ``False``), so numeric arguments must be defended here. A
    missing/null value falls back to ``default``; an int (or integral float, or
    digit string) is used directly; anything else raises a clean ``ValueError``
    instead of crashing later with a ``TypeError`` from ``min()``/``max()``.
    """
    if value is None:
        result = default
    elif isinstance(value, bool):
        # bool is an int subclass; reject it explicitly as a non-integer.
        raise ValueError("expected an integer, got a boolean")
    elif isinstance(value, int):
        result = value
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        result = int(value.strip())
    else:
        raise ValueError(f"expected an integer, got {type(value).__name__}")
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


# Fields an external agent may override when cloning/linking a task tree. Excludes
# identity/structural columns (user_id, id, task_tree_id, original_task_id, parent_id,
# dependencies, origin_type, status, schedule_*) — splatting those into the creator
# would let an agent reassign tenancy or corrupt tree/lineage invariants. Mirrors the
# TaskUpdateModule._UPDATABLE_FIELDS allowlist for the reuse path.
_OVERRIDABLE_REUSE_FIELDS = ("name", "inputs", "params", "priority")


def _filter_reuse_overrides(overrides: Any) -> dict[str, Any]:
    """Restrict external clone overrides to the safe, content-only allowlist."""
    if overrides is None:
        return {}
    if not isinstance(overrides, dict):
        raise ValueError("overrides must be an object")
    return _coerce_int_fields(
        {k: v for k, v in overrides.items() if k in _OVERRIDABLE_REUSE_FIELDS}
    )


# Per-task fields an external agent may set when creating a task tree from a raw
# array. Excludes origin_type/original_task_id/user_id — without this allowlist an
# agent could smuggle origin_type=link + original_task_id=<victim task> straight
# into build_task(), bypassing from_link's ownership and completion checks entirely.
_TASK_TREE_ITEM_FIELDS = (
    "id",
    "name",
    "parent_id",
    "priority",
    "inputs",
    "params",
    "dependencies",
    "token_budget",
    "cost_policy",
    "max_attempts",
)

# (minimum, maximum) bounds for integer fields accepted from external MCP/A2A
# callers, mirroring each field's schema. These arrive without schema
# validation (apcore-mcp validate_inputs=False), so — unlike sibling
# limit/offset/max_runs fields, which already go through _coerce_int — they
# must be coerced/validated here before ever reaching persistence.
_INT_FIELD_BOUNDS: dict[str, tuple[int | None, int | None]] = {
    "priority": (0, 3),
    "token_budget": (0, None),
    "max_attempts": (1, 100),
}


def _coerce_int_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce/validate priority, token_budget, and max_attempts in place."""
    for field, (minimum, maximum) in _INT_FIELD_BOUNDS.items():
        if field in data and data[field] is not None:
            data[field] = _coerce_int(data[field], data[field], minimum=minimum, maximum=maximum)
    return data


def _filter_task_tree_item(task: dict[str, Any]) -> dict[str, Any]:
    """Restrict a raw task-array item to the schema-advertised allowlist."""
    return _coerce_int_fields({k: v for k, v in task.items() if k in _TASK_TREE_ITEM_FIELDS})


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
    annotations = ModuleAnnotations()

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
        _coerce_int_fields(task_data)

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
    """Execute an existing task (and its tree) in the apflow task engine.

    A streaming module: ``stream()`` relays the engine's progress events
    (task_start / progress / task_completed / final) as they occur, then a
    terminal ``result`` event; ``execute()`` is the non-streaming equivalent.
    """

    description = "Execute an existing task in the apflow task engine."
    annotations = ModuleAnnotations(destructive=True, requires_approval=True, streaming=True)

    def __init__(self, task_manager: Any) -> None:
        # task_manager is retained for dependency-injection compatibility; execution
        # is driven through TaskExecutor, which owns the session and task-tree build.
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

    @staticmethod
    def _new_executor() -> Any:
        from apflow.core.execution.task_executor import TaskExecutor

        return TaskExecutor()

    async def preview(self, inputs: dict[str, Any], context: Any = None) -> PreviewResult:
        task_id = inputs.get("task_id", "")
        summary = f"Execute task '{task_id}'" if task_id else "Execute task (task_id not provided)"
        return PreviewResult(
            changes=[Change(action="execute", target=f"task:{task_id}", summary=summary)]
        )

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")
        return await self._new_executor().execute_task_by_id(task_id)

    async def stream(self, inputs: dict[str, Any], context: Any) -> AsyncIterator[dict[str, Any]]:
        """Execute the task, relaying engine progress events as they occur."""
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        events: asyncio.Queue = asyncio.Queue()
        run = asyncio.create_task(
            self._new_executor().execute_task_by_id(
                task_id, use_streaming=True, streaming_callbacks_context=events
            )
        )
        try:
            # Relay progress events until execution finishes.
            while not run.done():
                try:
                    yield await asyncio.wait_for(events.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
            # StreamingCallbacks enqueues events via detached fire-and-forget tasks, so
            # a late put (e.g. the terminal progress event) can land just after run
            # finishes. Drain with a short grace window instead of a single empty()
            # check so those in-flight events are not dropped.
            while True:
                try:
                    yield await asyncio.wait_for(events.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    break

            # Terminal event carrying the execution result (propagates execution errors).
            result = await run
            yield {"type": "result", "task_id": task_id, "result": result}
        finally:
            # On early generator close (SSE client disconnect -> GeneratorExit), cancel
            # the background execution so it does not keep running detached and holding
            # its pooled DB session. On the happy path run is already done -> no-op.
            if not run.done():
                run.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await run


class TaskListModule:
    """List tasks from the apflow task engine with optional filtering."""

    description = "List tasks from the apflow task engine with optional filtering."
    annotations = ModuleAnnotations(readonly=True, idempotent=True, paginated=True)

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
        limit = _coerce_int(inputs.get("limit"), 50, minimum=1, maximum=1000)
        offset = _coerce_int(inputs.get("offset"), 0, minimum=0)

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
    annotations = ModuleAnnotations(readonly=True, idempotent=True)

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
    annotations = ModuleAnnotations(destructive=True, requires_approval=True)

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

    async def preview(self, inputs: dict[str, Any], context: Any = None) -> PreviewResult:
        task_id = inputs.get("task_id", "")
        summary = (
            f"Permanently delete task '{task_id}'"
            if task_id
            else "Permanently delete task (task_id not provided)"
        )
        return PreviewResult(
            changes=[Change(action="delete", target=f"task:{task_id}", summary=summary)]
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
                    "id": {
                        "type": "string",
                        "description": "Optional task ID (auto-generated if omitted)",
                    },
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Task name (required)",
                    },
                    "parent_id": {
                        "type": "string",
                        "description": "Parent task ID for tree structure",
                    },
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
    annotations = ModuleAnnotations()

    def __init__(self, task_creator: Any, task_repository: Any) -> None:
        self._creator = task_creator
        self._repo = task_repository
        self.input_schema = _make_schema(_TASK_CREATE_TREE_INPUT)
        self.output_schema = _make_schema(_TASK_CREATE_TREE_OUTPUT)

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        raw_tasks = inputs.get("tasks", [])
        if not isinstance(raw_tasks, list):
            raise ValueError("tasks must be an array")
        if not raw_tasks:
            raise ValueError("tasks array must be non-empty")

        tasks: list[dict[str, Any]] = []
        for index, t in enumerate(raw_tasks):
            if not isinstance(t, dict):
                raise ValueError(f"tasks[{index}] must be an object")
            if not t.get("name"):
                raise ValueError("Each task must have a non-empty 'name'")
            tasks.append(_filter_task_tree_item(t))

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


_TASK_REUSE_INPUT = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "minLength": 1,
            "description": "ID of the existing task to reuse",
        },
        "recursive": {
            "type": "boolean",
            "default": True,
            "description": "If true, reuse entire subtree; if false, only the single task",
        },
        "auto_include_deps": {
            "type": "boolean",
            "default": True,
            "description": "Automatically include upstream dependency tasks",
        },
        "overrides": {
            "type": "object",
            "description": "Fields to override (e.g. inputs, priority, user_id)",
        },
    },
    "required": ["task_id"],
}

_TASK_REUSE_OUTPUT = {
    "type": "object",
    "properties": {
        "root_task_id": {"type": "string"},
        "task_count": {"type": "integer"},
        "origin_type": {"type": "string"},
    },
}


class TaskLinkModule:
    """Link to a completed workflow — read-only reference, zero storage cost."""

    description = (
        "Create a read-only reference to a completed task tree. "
        "The linked tasks point to the originals without duplicating data. "
        "Requires the source task tree to be fully completed."
    )
    annotations = ModuleAnnotations(idempotent=True)

    def __init__(self, task_creator: Any, task_repository: Any) -> None:
        self._creator = task_creator
        self._repo = task_repository
        self.input_schema = _make_schema(_TASK_REUSE_INPUT)
        self.output_schema = _make_schema(_TASK_REUSE_OUTPUT)

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        task = await self._repo.get_task_by_id(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")

        overrides = _filter_reuse_overrides(inputs.get("overrides"))
        tree = await self._creator.from_link(
            task,
            _recursive=inputs.get("recursive", True),
            _auto_include_deps=inputs.get("auto_include_deps", True),
            **overrides,
        )

        return {
            "root_task_id": tree.task.id,
            "task_count": len(tree.to_list()),
            "origin_type": "link",
        }


class TaskCopyModule:
    """Copy a workflow — create a modifiable clone with optional overrides."""

    description = (
        "Clone an existing task tree with new UUIDs. All dependencies are "
        "automatically remapped. Override any field (inputs, priority, etc.) "
        "to create a variant. Use this to re-run a workflow with different parameters."
    )
    annotations = ModuleAnnotations()

    def __init__(self, task_creator: Any, task_repository: Any) -> None:
        self._creator = task_creator
        self._repo = task_repository
        self.input_schema = _make_schema(_TASK_REUSE_INPUT)
        self.output_schema = _make_schema(_TASK_REUSE_OUTPUT)

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        task = await self._repo.get_task_by_id(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")

        overrides = _filter_reuse_overrides(inputs.get("overrides"))
        tree = await self._creator.from_copy(
            task,
            _recursive=inputs.get("recursive", True),
            _auto_include_deps=inputs.get("auto_include_deps", True),
            **overrides,
        )

        return {
            "root_task_id": tree.task.id,
            "task_count": len(tree.to_list()),
            "origin_type": "copy",
        }


class TaskArchiveModule:
    """Archive a completed workflow — create a frozen, immutable snapshot."""

    description = (
        "Freeze a completed task tree as an immutable archive. "
        "Preserves all data including results. Used for audit trails, "
        "compliance records, and production snapshots. "
        "Requires the source task tree to be fully completed."
    )
    annotations = ModuleAnnotations(idempotent=True)

    def __init__(self, task_creator: Any, task_repository: Any) -> None:
        self._creator = task_creator
        self._repo = task_repository
        self.input_schema = _make_schema(_TASK_REUSE_INPUT)
        self.output_schema = _make_schema(_TASK_REUSE_OUTPUT)

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        task = await self._repo.get_task_by_id(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")

        tree = await self._creator.from_archive(
            task,
            _recursive=inputs.get("recursive", True),
            _auto_include_deps=inputs.get("auto_include_deps", True),
        )

        return {
            "root_task_id": tree.task.id,
            "task_count": len(tree.to_list()),
            "origin_type": "archive",
        }


class TaskCloneMixedModule:
    """Clone with mixed mode — partial copy + partial link in one tree."""

    description = (
        "Clone a task tree with mixed origin types: some tasks are copied (modifiable), "
        "others are linked (read-only reference). Specify link_task_ids to choose which "
        "tasks to link; all others are copied. Use this to re-run only changed steps."
    )
    annotations = ModuleAnnotations()

    def __init__(self, task_creator: Any, task_repository: Any) -> None:
        self._creator = task_creator
        self._repo = task_repository
        self.input_schema = _make_schema(
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 1, "description": "Source task ID"},
                    "link_task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Task IDs to link (reference). Others will be copied.",
                    },
                    "recursive": {"type": "boolean", "default": True},
                    "overrides": {
                        "type": "object",
                        "description": "Fields to override on copied tasks",
                    },
                },
                "required": ["task_id", "link_task_ids"],
            }
        )
        self.output_schema = _make_schema(_TASK_REUSE_OUTPUT)

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        task = await self._repo.get_task_by_id(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")

        overrides = _filter_reuse_overrides(inputs.get("overrides"))
        tree = await self._creator.from_mixed(
            task,
            _recursive=inputs.get("recursive", True),
            _link_task_ids=inputs.get("link_task_ids", []),
            **overrides,
        )

        return {
            "root_task_id": tree.task.id,
            "task_count": len(tree.to_list()),
            "origin_type": "mixed",
        }


class TaskUpdateModule:
    """Update fields on an existing task."""

    # Only these schema-advertised fields may be written. External inputs are not
    # schema-validated by default (apcore-mcp validate_inputs=False), so the
    # writable surface must be enforced here rather than splatting arbitrary keys
    # into update_task() (which would let an agent set e.g. user_id). (Review W1)
    annotations = ModuleAnnotations()
    _UPDATABLE_FIELDS = (
        "name",
        "status",
        "priority",
        "inputs",
        "params",
        "error",
        "result",
        "progress",
    )

    description = (
        "Update one or more fields on an existing task. Can update name, status, priority, "
        "inputs, params, error, result, and progress. Use schedule.set to configure a task's "
        "schedule (schedule_type, schedule_expression, etc.) — those fields are not writable here."
    )

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository
        self.input_schema = _make_schema(
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 1},
                    "name": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "failed", "cancelled"],
                    },
                    "priority": {"type": "integer", "minimum": 0, "maximum": 3},
                    "inputs": {"type": "object"},
                    "params": {"type": "object"},
                    "error": {"type": "string"},
                    "result": {"type": "object"},
                    "progress": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["task_id"],
            }
        )
        self.output_schema = _make_schema({"type": "object"})

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        task = await self._repo.get_task_by_id(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")

        update_fields = {
            field: inputs[field]
            for field in self._UPDATABLE_FIELDS
            if inputs.get(field) is not None
        }
        _coerce_int_fields(update_fields)
        if update_fields:
            await self._repo.update_task(task_id=task_id, **update_fields)

        task = await self._repo.get_task_by_id(task_id)
        return task.to_dict()


class TaskCancelModule:
    """Cancel one or more running tasks."""

    description = (
        "Cancel running tasks by ID. Returns cancellation status for each task. "
        "Supports partial results and token usage from cancelled executors."
    )
    annotations = ModuleAnnotations(destructive=True, requires_approval=True)

    def __init__(self, task_manager: Any) -> None:
        self._manager = task_manager
        self.input_schema = _make_schema(
            {
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "List of task IDs to cancel",
                    },
                    "error_message": {
                        "type": "string",
                        "description": "Custom cancellation message",
                    },
                },
                "required": ["task_ids"],
            }
        )
        self.output_schema = _make_schema(
            {
                "type": "object",
                "properties": {
                    "results": {"type": "array", "items": {"type": "object"}},
                },
            }
        )

    async def preview(self, inputs: dict[str, Any], context: Any = None) -> PreviewResult:
        task_ids = inputs.get("task_ids") or []
        changes = [
            Change(
                action="cancel",
                target=f"task:{tid}",
                summary=f"Cancel running task '{tid}'",
            )
            for tid in task_ids
        ]
        return PreviewResult(changes=changes)

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        task_ids = inputs.get("task_ids", [])
        if not isinstance(task_ids, list):
            raise ValueError("task_ids must be an array")
        if not task_ids:
            raise ValueError("task_ids must be non-empty")

        error_message = inputs.get("error_message", "Cancelled via API")
        results = []
        for tid in task_ids:
            try:
                result = await self._manager.cancel_task(tid, error_message=error_message)
                results.append({"task_id": tid, **result})
            except Exception as e:
                results.append({"task_id": tid, "status": "failed", "message": str(e)})

        return {"results": results}


class TaskTreeModule:
    """Get the full tree structure of a task."""

    description = (
        "Get the complete task tree starting from a root task, including all children "
        "and their statuses. Returns nested tree structure."
    )
    annotations = ModuleAnnotations(readonly=True, idempotent=True)

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

        tree = await self._repo.build_task_tree(task)
        return tree.output()


class TaskChildrenModule:
    """Get direct children of a task."""

    description = "Get the direct children of a task by parent ID."
    annotations = ModuleAnnotations(readonly=True, idempotent=True)

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository
        self.input_schema = _make_schema(_TASK_ID_INPUT)
        self.output_schema = _make_schema(
            {
                "type": "object",
                "properties": {"children": {"type": "array", "items": {"type": "object"}}},
            }
        )

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        children = await self._repo.get_child_tasks_by_parent_id(task_id)
        return {
            "children": [
                {"id": c.id, "name": c.name, "status": c.status, "priority": c.priority}
                for c in children
            ]
        }


class TaskRunningListModule:
    """List currently running tasks."""

    description = "List all tasks currently in 'in_progress' status."
    annotations = ModuleAnnotations(readonly=True, idempotent=True, paginated=True)

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository
        self.input_schema = _make_schema(
            {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
            }
        )
        self.output_schema = _make_schema(
            {
                "type": "object",
                "properties": {"tasks": {"type": "array"}, "count": {"type": "integer"}},
            }
        )

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        tasks = await self._repo.query_tasks(
            status="in_progress",
            user_id=inputs.get("user_id"),
            limit=_coerce_int(inputs.get("limit"), 20, minimum=1, maximum=100),
        )
        return {
            "tasks": [{"id": t.id, "name": t.name, "status": t.status} for t in tasks],
            "count": len(tasks),
        }


class TaskScheduledListModule:
    """List scheduled tasks."""

    description = "List tasks that have scheduling configured (cron, interval, etc.)."
    annotations = ModuleAnnotations(readonly=True, idempotent=True, paginated=True)

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository
        self.input_schema = _make_schema(
            {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "enabled_only": {
                        "type": "boolean",
                        "default": True,
                        "description": "Only show enabled schedules",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
            }
        )
        self.output_schema = _make_schema(
            {
                "type": "object",
                "properties": {"tasks": {"type": "array"}, "count": {"type": "integer"}},
            }
        )

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        # Filter scheduled tasks (and scope by user_id) at the DB layer so `limit`
        # bounds the scheduled result set rather than an unscheduled pre-filter page,
        # matching the user_id scoping applied by the sibling list modules.
        limit = _coerce_int(inputs.get("limit"), 20, minimum=1, maximum=100)
        scheduled = await self._repo.get_scheduled_tasks(
            enabled_only=bool(inputs.get("enabled_only", True)),
            user_id=inputs.get("user_id"),
            limit=limit,
        )
        return {
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "schedule_type": t.schedule_type,
                    "schedule_expression": t.schedule_expression,
                    "schedule_enabled": t.schedule_enabled,
                    "next_run_at": str(t.next_run_at) if t.next_run_at else None,
                }
                for t in scheduled
            ],
            "count": len(scheduled),
        }
