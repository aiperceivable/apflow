"""
Scheduling modules for apcore registration.

Expose apflow's task-scheduling lifecycle as duck-typed apcore modules so they
appear as REST endpoints, MCP tools, A2A skills, and CLI commands:

    schedule.set          -> configure a task's schedule + compute next run
    schedule.due          -> list scheduled tasks whose next run has arrived
    schedule.trigger      -> execute a scheduled task now (inbound webhook)
    schedule.complete     -> record a run complete + advance the next run
    schedule.export_ical  -> export scheduled tasks as an iCalendar feed

The long-running scheduler daemon (start/stop/pause) is intentionally NOT a
module — it is a lifecycle process (run it via ``apflow scheduler``), not a
stateless request/response call. Listing configured schedules already exists
as the ``task.scheduled`` module.

Repository methods are async (AsyncSession); execute() awaits them.
"""

from typing import Any

from apcore import ModuleAnnotations

from apflow.bridge.task_modules import _coerce_int, _make_schema


class ScheduleSetModule:
    """Configure (or update) a task's schedule and compute its next run time."""

    description = "Set a task's schedule (type + expression) and compute its next run time."
    annotations = ModuleAnnotations()

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository
        self.input_schema = _make_schema(
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 1, "description": "Task ID"},
                    "schedule_type": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Schedule kind, e.g. 'cron' or 'interval'",
                    },
                    "schedule_expression": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Cron expression or interval spec",
                    },
                    "schedule_enabled": {"type": "boolean", "default": True},
                    "max_runs": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Optional cap on total runs",
                    },
                },
                "required": ["task_id", "schedule_type", "schedule_expression"],
            }
        )
        self.output_schema = _make_schema({"type": "object"})

    async def execute(self, inputs: dict[str, Any], _context: Any = None) -> dict[str, Any]:
        from apflow.core.storage.sqlalchemy.schedule_calculator import ScheduleCalculator

        task_id = inputs.get("task_id", "")
        schedule_type = inputs.get("schedule_type", "")
        schedule_expression = inputs.get("schedule_expression", "")
        if not task_id or not schedule_type or not schedule_expression:
            raise ValueError("task_id, schedule_type and schedule_expression are required")

        # Validate the schedule BEFORE persisting, so an invalid type/expression never
        # leaves a half-applied schedule on the task and surfaces a clear error rather
        # than a misleading "task not found" (initialize_schedule swallows the calc
        # failure into None, which would otherwise be read as a missing task below).
        try:
            ScheduleCalculator.calculate_next_run(schedule_type, schedule_expression)
        except ValueError as exc:
            raise ValueError(f"Invalid schedule: {exc}") from exc

        # Confirm the task exists before mutating: a genuine miss raises KeyError here,
        # so a later None from initialize_schedule can only mean a real lookup miss.
        if await self._repo.get_task_by_id(task_id) is None:
            raise KeyError(f"Task '{task_id}' not found")

        fields: dict[str, Any] = {
            "schedule_type": schedule_type,
            "schedule_expression": schedule_expression,
            "schedule_enabled": bool(inputs.get("schedule_enabled", True)),
        }
        if inputs.get("max_runs") is not None:
            fields["max_runs"] = _coerce_int(inputs.get("max_runs"), 1, minimum=1)

        await self._repo.update_task(task_id=task_id, **fields)
        task = await self._repo.initialize_schedule(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' not found")
        return task.to_dict()


class ScheduleDueModule:
    """List scheduled tasks that are due to run now."""

    description = "List scheduled tasks whose next run time has arrived (for external schedulers)."
    annotations = ModuleAnnotations(readonly=True, idempotent=True, paginated=True)

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository
        self.input_schema = _make_schema(
            {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                },
            }
        )
        self.output_schema = _make_schema(
            {
                "type": "object",
                "properties": {"tasks": {"type": "array"}, "count": {"type": "integer"}},
            }
        )

    async def execute(self, inputs: dict[str, Any], _context: Any = None) -> dict[str, Any]:
        tasks = await self._repo.get_due_scheduled_tasks(
            user_id=inputs.get("user_id"),
            limit=_coerce_int(inputs.get("limit"), 100, minimum=1, maximum=1000),
        )
        return {
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "schedule_type": t.schedule_type,
                    "schedule_expression": t.schedule_expression,
                    "next_run_at": str(t.next_run_at) if t.next_run_at else None,
                }
                for t in tasks
            ],
            "count": len(tasks),
        }


class ScheduleCompleteModule:
    """Record a scheduled run as complete and advance the next run time."""

    description = (
        "Record a scheduled run as complete and compute the next run time "
        "(for external schedulers)."
    )
    annotations = ModuleAnnotations()

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository
        self.input_schema = _make_schema(
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 1, "description": "Task ID"},
                    "success": {"type": "boolean", "default": True},
                    "error": {"type": "string"},
                    "calculate_next_run": {"type": "boolean", "default": True},
                },
                "required": ["task_id"],
            }
        )
        self.output_schema = _make_schema({"type": "object"})

    async def execute(self, inputs: dict[str, Any], _context: Any = None) -> dict[str, Any]:
        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        # complete_scheduled_run returns None for BOTH a missing task and a swallowed
        # failure (e.g. a corrupt stored schedule). Check existence first so an existing
        # task is never misreported as "not found"; a None after that is an operation
        # failure, surfaced distinctly rather than as a 404.
        if await self._repo.get_task_by_id(task_id) is None:
            raise KeyError(f"Task '{task_id}' not found")

        task = await self._repo.complete_scheduled_run(
            task_id=task_id,
            success=bool(inputs.get("success", True)),
            error=inputs.get("error"),
            calculate_next_run=bool(inputs.get("calculate_next_run", True)),
        )
        if task is None:
            raise RuntimeError(
                f"Failed to complete scheduled run for task '{task_id}' "
                "(check the task's schedule configuration)"
            )
        return task.to_dict()


class ScheduleTriggerModule:
    """Trigger immediate execution of a scheduled task.

    This is the registry-native form of the inbound webhook: external schedulers
    (cron, Kubernetes CronJob, Temporal, ...) call it to push-trigger a task,
    complementing the built-in ``apflow scheduler`` poll loop's pull model. It
    reuses ``WebhookGateway``, which marks the task running, executes its tree,
    and advances the next run — atomically on the server side.
    """

    description = (
        "Trigger a scheduled task to execute now (inbound webhook for external "
        "schedulers such as cron or Kubernetes CronJob)."
    )
    annotations = ModuleAnnotations()

    def __init__(self, task_repository: Any) -> None:
        self._repo = task_repository
        self.input_schema = _make_schema(
            {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Task ID to trigger",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "Optional owner check; rejects tasks owned by others",
                    },
                    # Intentionally defaults to False (synchronous) — overrides
                    # WebhookConfig's True default so callers get a result immediately.
                    "async_execution": {
                        "type": "boolean",
                        "default": False,
                        "description": "Return immediately and run in the background",
                    },
                },
                "required": ["task_id"],
            }
        )
        self.output_schema = _make_schema({"type": "object"})

    async def execute(self, inputs: dict[str, Any], _context: Any = None) -> dict[str, Any]:
        from apflow.scheduler.gateway.webhook import WebhookGateway

        task_id = inputs.get("task_id", "")
        if not task_id:
            raise ValueError("task_id must be non-empty")

        # Surface a genuine miss as a distinct KeyError (REST maps it to 404) rather
        # than the gateway's generic {"success": False} payload. A task's own
        # execution failure is a valid result (success=False), not a lookup error.
        if await self._repo.get_task_by_id(task_id) is None:
            raise KeyError(f"Task '{task_id}' not found")

        gateway = WebhookGateway()
        return await gateway.trigger_task(
            task_id,
            user_id=inputs.get("user_id"),
            execute_async=bool(inputs.get("async_execution", False)),
        )


class ScheduleExportICalModule:
    """Export scheduled tasks as an iCalendar feed."""

    description = "Export scheduled tasks as an iCalendar (.ics) feed."
    annotations = ModuleAnnotations(readonly=True, idempotent=True)

    def __init__(self) -> None:
        self.input_schema = _make_schema(
            {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "schedule_type": {"type": "string"},
                    "enabled_only": {"type": "boolean", "default": True},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                    "base_url": {
                        "type": "string",
                        "description": "Base URL for task links in the feed",
                    },
                },
            }
        )
        self.output_schema = _make_schema(
            {
                "type": "object",
                "properties": {"ical": {"type": "string"}, "format": {"type": "string"}},
            }
        )

    async def execute(self, inputs: dict[str, Any], _context: Any = None) -> dict[str, Any]:
        from apflow.scheduler.gateway.ical import ICalExporter

        exporter = ICalExporter(base_url=inputs.get("base_url"))
        ical = await exporter.export_tasks(
            user_id=inputs.get("user_id"),
            schedule_type=inputs.get("schedule_type"),
            enabled_only=bool(inputs.get("enabled_only", True)),
            limit=_coerce_int(inputs.get("limit"), 100, minimum=1, maximum=1000),
        )
        return {"ical": ical, "format": "text/calendar"}
