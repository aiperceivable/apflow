"""
SQLAlchemy models for task storage
"""

from sqlalchemy import (
    String,
    Integer,
    DateTime,
    JSON,
    Text,
    Boolean,
    Numeric,
    ForeignKey,
    Index,
    event,
    inspect,
    text,
)
from sqlalchemy.sql import func
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from typing import Dict, Any, Optional, Self, TypeVar
from enum import auto, StrEnum
import re as _re
import uuid
import os
from datetime import datetime, timezone
from decimal import Decimal
from copy import deepcopy

from apflow.core.storage.sqlalchemy.invariants import register_storage_invariants


class Base(DeclarativeBase):
    """Shared declarative registry for storage models."""


# Type variable for TaskModel subclasses
TaskModelType = TypeVar("TaskModelType", bound="TaskModel")

# Table name configuration - supports environment variable override
# Default: "apflow_tasks" (apflow tasks)
# Can be overridden via APFLOW_TASK_TABLE_NAME environment variable
TASK_TABLE_NAME = os.getenv("APFLOW_TASK_TABLE_NAME", "apflow_tasks")
if not _re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", TASK_TABLE_NAME):
    raise ValueError(
        f"Invalid APFLOW_TASK_TABLE_NAME: '{TASK_TABLE_NAME}'. "
        f"Must be a valid SQL identifier (letters, digits, underscores)."
    )


class SubmissionReceipt(Base):
    """Atomic, subject-scoped receipt for an idempotent task submission."""

    __tablename__ = "apflow_submission_receipts"

    scope: Mapped[str] = mapped_column(String(255), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class TaskDispatch(Base):
    """Persistent acceptance and lease state, separate from task definitions."""

    __tablename__ = "apflow_task_dispatch"

    task_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    state: Mapped[str] = mapped_column(String(50), default="accepted", nullable=False)
    attempt_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    owner_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    identity: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BudgetAccount(Base):
    """Shared token balance; unknown liabilities remain held until reconciliation."""

    __tablename__ = "apflow_budget_accounts"

    scope_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    limit_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    spent_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unknown_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class BudgetReservation(Base):
    """One invocation's reservation and idempotent settlement record."""

    __tablename__ = "apflow_budget_reservations"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(20), default="reserved", nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExecutionAttempt(Base):
    """One engine invocation with bounded failure metadata and independently reported usage."""

    __tablename__ = "apflow_execution_attempts"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    attempt_id: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    error_info: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    token_usage: Mapped[dict[str, int] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowRun(Base):
    """An immutable graph snapshot and lifecycle for one accepted execution."""

    __tablename__ = "apflow_workflow_runs"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    root_task_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="accepted")
    definition_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    retry_of_run_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        """Serialize lifecycle timestamps consistently across protocol adapters."""
        result = {column.name: getattr(self, column.name) for column in self.__table__.columns}
        for name in ("created_at", "started_at", "completed_at"):
            value = result[name]
            result[name] = (
                value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat() if value else None
            )
        return result


class UsageEvent(Base):
    """Immutable consumption evidence and a transactional delivery marker."""

    __tablename__ = "apflow_usage_events"
    __table_args__ = {"sqlite_autoincrement": True}

    index: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    invocation_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    reported_usage: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False)
    token_usage: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


@event.listens_for(UsageEvent, "before_update")
def _guard_usage_event_update(mapper: Any, connection: Connection, target: UsageEvent) -> None:
    """Only a delivery acknowledgement may change an existing consumption row."""
    state = inspect(target)
    if any(
        state.attrs[column.name].history.has_changes()
        for column in UsageEvent.__table__.columns
        if column.name != "delivered_at"
    ):
        raise ValueError("Usage consumption facts are immutable")


@event.listens_for(UsageEvent, "before_delete")
def _guard_usage_event_delete(mapper: Any, connection: Connection, target: UsageEvent) -> None:
    raise ValueError("Usage consumption facts cannot be deleted")


class TaskOriginType(StrEnum):
    create = auto()  # Task created freshly
    link = auto()  # Task linked from another
    copy = auto()  # Task copied from another (can be modified)
    archive = auto()  # Task archived from another (can not be modified)
    scheduled_run = auto()  # Frozen run record snapshotted by the scheduler on each fire


class ScheduleType(StrEnum):
    """Schedule type for recurring tasks"""

    once = auto()  # Single execution at a specific datetime
    interval = auto()  # Recurring at fixed intervals (in seconds)
    cron = auto()  # Cron expression scheduling
    daily = auto()  # Daily at specific time
    weekly = auto()  # Weekly on specific days
    monthly = auto()  # Monthly on specific dates


class TaskModel(Base):
    """
    Task Definition Model - Handles task orchestration and definition

    This model represents task definitions for orchestration. A2A Protocol Task represents
    execution instances with LLM message context.

    Key design:
    - TaskModel: Task definition (static, orchestration)
    - A2A Protocol Task: Task execution instance (dynamic, LLM context)

    Mapping relationship:
    - TaskModel.id -> A2A Task.context_id (task definition ID = context ID)
    - A2A Task.id -> Execution instance ID (A2A Protocol internal, auto-generated)
    - One TaskModel can have multiple Task execution instances

    Note: A2A Protocol Task fields (artifacts, history, kind, metadata) are execution-level
    and should NOT be stored in TaskModel. They are managed by A2A Protocol TaskStore.

    Table name: Configurable via APFLOW_TASK_TABLE_NAME environment variable.
    Default: "apflow_tasks" - prefixed to distinguish from A2A Protocol's "tasks" table.
    This table stores both task definitions (orchestration) and execution results.
    """

    __tablename__ = TASK_TABLE_NAME  # Configurable table name (default: "apflow_tasks")

    # Partial unique index on idempotency_key: enforces effectively-once dispatch
    # for scheduled runs (a duplicate dispatch of the same occurrence carries the
    # same deterministic key and is rejected at INSERT). Only rows that set a key
    # are constrained — regular tasks and manual triggers leave it NULL and are
    # unaffected.
    __table_args__ = (
        Index(
            f"uq_{TASK_TABLE_NAME}_idempotency_key",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    # === Task Definition Identity ===
    id: Mapped[str] = mapped_column(
        String(255), primary_key=True, index=True, default=lambda: str(uuid.uuid4()), nullable=False
    )  # Task definition ID (maps to A2A Task.context_id)

    # === Task Tree Structure (TaskManager) ===
    parent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )  # Parent task ID (for task tree hierarchy)
    task_tree_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )  # Task tree identifier (for grouping/querying across trees)
    run_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    result_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # === User Identification (Optional, Multi-user Support) ===
    user_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )  # User ID (optional, for multi-user scenarios)
    execution_identity: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # === Task Basic Information ===
    name: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )  # Task name/method identifier
    status: Mapped[str] = mapped_column(
        String(50), default="pending", index=True, nullable=True
    )  # Task status: pending, in_progress, completed, failed, cancelled

    # === Task Orchestration (TaskManager) ===
    priority: Mapped[int] = mapped_column(
        Integer, default=1, nullable=True
    )  # Priority level: 0=urgent (highest), 1=high (default), 2=normal, 3=low (lowest). ASC order: smaller numbers execute first. NOTE: create() always injects default_values()['priority']=1, so this column default aligns with it.
    dependencies: Mapped[Any] = mapped_column(
        JSON, nullable=True
    )  # Task dependencies: [{"id": "uuid", "required": true}]

    # === Task Data ===
    inputs: Mapped[Any] = mapped_column(
        JSON, nullable=True
    )  # Execution-time input parameters for executor.execute(inputs)
    params: Mapped[Any] = mapped_column(
        JSON, nullable=True
    )  # Executor initialization parameters for executor.__init__(**params)
    result: Mapped[Any] = mapped_column(
        JSON, nullable=True
    )  # Latest business output, normalized at the executor boundary
    error: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Execution failure message, separate from business output
    error_info: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    schemas: Mapped[Any] = mapped_column(
        JSON, nullable=True
    )  # Validation schemas (input_schema, output_schema)

    # Note: A2A Protocol execution fields (artifacts, history, kind, metadata) are NOT stored here.
    # They are managed by A2A Protocol TaskStore as execution instances.

    # === Task Progress ===
    progress: Mapped[Decimal | float] = mapped_column(
        Numeric(3, 2), default=0.0, nullable=True
    )  # Progress as decimal (0.00 to 1.00)

    # === Timestamps ===
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # === Auxiliary Fields ===
    has_children: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=True
    )  # UI/performance optimization flag

    # === Task Origin/Reference Fields ===
    original_task_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )  # Source task ID (for copy/reference/link)
    origin_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, default=None, index=True
    )  # Origin kind: TaskOriginType
    has_references: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True, nullable=True
    )  # Whether this task is referenced/copied by others

    # === Scheduling Configuration ===
    schedule_type: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )  # Schedule type: ScheduleType (once, interval, cron, daily, weekly, monthly)
    schedule_expression: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )  # Schedule expression (format depends on schedule_type)
    schedule_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True, nullable=True
    )  # Whether scheduling is enabled for this task

    # === Schedule Boundaries ===
    schedule_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Earliest time the schedule can trigger
    schedule_end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Latest time the schedule can trigger (after this, schedule is disabled)

    # === Schedule State ===
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )  # Next scheduled execution time (calculated field)
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # Last time this scheduled task was executed

    # === Execution Control ===
    max_runs: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Maximum number of scheduled runs (null = unlimited)
    run_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=True
    )  # Number of times this scheduled task has been executed

    # === Distributed Execution Fields (nullable, backward compatible) ===
    lease_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    placement_constraints: Mapped[Any] = mapped_column(JSON, nullable=True)
    attempt_id: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_assigned_node: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # === Durability Fields (F-003) ===
    checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resume_from: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=True)
    backoff_strategy: Mapped[str] = mapped_column(String(20), default="exponential", nullable=True)
    backoff_base_seconds: Mapped[Decimal | float] = mapped_column(
        Numeric(10, 2), default=1.0, nullable=True
    )

    # === Cost Governance Fields (F-004) ===
    token_usage: Mapped[Any] = mapped_column(JSON, nullable=True)
    token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    budget_scope_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    estimated_cost_usd: Mapped[Decimal | float | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )
    actual_cost_usd: Mapped[Decimal | float | None] = mapped_column(Numeric(12, 6), nullable=True)
    cost_policy: Mapped[str | None] = mapped_column(String(100), nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        """Convert model to dictionary"""
        return {
            # Task definition identity
            "id": self.id,
            # Task tree structure
            "parent_id": self.parent_id,
            "task_tree_id": self.task_tree_id,
            "run_id": self.run_id,
            "source_task_id": self.source_task_id,
            "result_snapshot_hash": self.result_snapshot_hash,
            # User identification
            "user_id": self.user_id,
            # Task basic information
            "name": self.name,
            "status": self.status,
            # Task orchestration
            "priority": self.priority,
            "dependencies": self.dependencies,
            # Task data
            "inputs": self.inputs,
            "params": self.params,
            "result": self.result,  # Latest execution result
            "error": self.error,
            "error_info": self.error_info,
            "schemas": self.schemas,
            # Task progress
            "progress": float(self.progress) if self.progress is not None else 0.0,
            # Timestamps
            "created_at": self.created_at,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "cancel_requested_at": self.cancel_requested_at,
            # Auxiliary fields
            "has_children": self.has_children,
            # Task origin/reference fields
            "original_task_id": self.original_task_id,
            "origin_type": (
                self.origin_type.value
                if isinstance(self.origin_type, TaskOriginType)
                else self.origin_type
            ),
            "has_references": self.has_references,
            # Scheduling configuration
            "schedule_type": (
                self.schedule_type.value
                if isinstance(self.schedule_type, ScheduleType)
                else self.schedule_type
            ),
            "schedule_expression": self.schedule_expression,
            "schedule_enabled": self.schedule_enabled,
            # Schedule boundaries
            "schedule_start_at": self.schedule_start_at,
            "schedule_end_at": self.schedule_end_at,
            # Schedule state
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            # Execution control
            "max_runs": self.max_runs,
            "run_count": self.run_count,
            # Distributed execution fields
            "lease_id": self.lease_id,
            "lease_expires_at": self.lease_expires_at,
            "placement_constraints": self.placement_constraints,
            "attempt_id": self.attempt_id,
            "idempotency_key": self.idempotency_key,
            "last_assigned_node": self.last_assigned_node,
            # Durability fields
            "checkpoint_at": self.checkpoint_at,
            "resume_from": self.resume_from,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "backoff_strategy": self.backoff_strategy,
            "backoff_base_seconds": (
                float(self.backoff_base_seconds) if self.backoff_base_seconds else 1.0
            ),
            # Cost governance fields
            "token_usage": self.token_usage,
            "token_budget": self.token_budget,
            "budget_scope_id": self.budget_scope_id,
            "estimated_cost_usd": (
                float(self.estimated_cost_usd) if self.estimated_cost_usd is not None else None
            ),
            "actual_cost_usd": (
                float(self.actual_cost_usd) if self.actual_cost_usd is not None else None
            ),
            "cost_policy": self.cost_policy,
        }

    def output(self) -> Dict[str, Any]:
        """Convert model to dictionary"""
        data = self.to_dict()
        data["created_at"] = self.created_at.isoformat() if self.created_at else None
        data["started_at"] = self.started_at.isoformat() if self.started_at else None
        data["updated_at"] = self.updated_at.isoformat() if self.updated_at else None
        data["completed_at"] = self.completed_at.isoformat() if self.completed_at else None
        data["cancel_requested_at"] = (
            self.cancel_requested_at.isoformat() if self.cancel_requested_at else None
        )
        # Schedule datetime fields
        data["schedule_start_at"] = (
            self.schedule_start_at.isoformat() if self.schedule_start_at else None
        )
        data["schedule_end_at"] = self.schedule_end_at.isoformat() if self.schedule_end_at else None
        data["next_run_at"] = self.next_run_at.isoformat() if self.next_run_at else None
        data["last_run_at"] = self.last_run_at.isoformat() if self.last_run_at else None
        return data

    def copy(self, override: Optional[Dict[str, Any]] = None) -> Self:
        """
        Return a new instance of this TaskModel (or subclass), optionally overriding fields.
        """
        data = self.to_dict()
        data["execution_identity"] = deepcopy(self.execution_identity)
        if override:
            data.update(override)
        if data.get("user_id") != self.user_id:
            data["execution_identity"] = None
        return self.__class__(**data)

    def update_from_dict(self, data: Dict[str, Any]) -> Self:
        """
        Update model fields from a dictionary
        """
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)

        if "status" in data:
            if data["status"] in {"completed", "failed", "cancelled"}:
                from datetime import datetime

                self.completed_at = datetime.now(timezone.utc)

        return self

    def default_values(self) -> Dict[str, Any]:
        """
        Return default values for TaskModel fields
        """
        return {
            "user_id": None,
            "execution_identity": None,
            "parent_id": None,
            "priority": 1,
            "dependencies": [],
            "inputs": {},
            "schemas": {},
            "params": {},
            "status": "pending",
            "progress": 0.0,
            "has_children": False,
            "cancel_requested_at": None,
            "error_info": None,
            "original_task_id": None,
            "origin_type": TaskOriginType.create,
            "task_tree_id": None,
            "has_references": False,
            # Scheduling defaults
            "schedule_type": None,
            "schedule_expression": None,
            "schedule_enabled": False,
            "schedule_start_at": None,
            "schedule_end_at": None,
            "next_run_at": None,
            "last_run_at": None,
            "max_runs": None,
            "run_count": 0,
            # Distributed execution defaults
            "lease_id": None,
            "lease_expires_at": None,
            "placement_constraints": None,
            "attempt_id": 0,
            "idempotency_key": None,
            "last_assigned_node": None,
            # Durability defaults
            "checkpoint_at": None,
            "resume_from": None,
            "attempt_count": 0,
            "max_attempts": 3,
            "backoff_strategy": "exponential",
            "backoff_base_seconds": 1.0,
            # Cost governance defaults
            "token_usage": None,
            "token_budget": None,
            "budget_scope_id": None,
            "estimated_cost_usd": None,
            "actual_cost_usd": None,
            "cost_policy": None,
        }

    @classmethod
    def create(cls: type[Self], data: Dict[str, Any]) -> Self:
        """
        Create a new TaskModel (or subclass) instance from a dictionary,
        applying default values and filtering unavailable columns.
        """
        available_columns = (
            {c.name for c in cls.__table__.columns} if hasattr(cls, "__table__") else set()
        )
        # Start with defaults, then update with provided data
        task_data = cls().default_values()
        task_data.update(data)
        # Only keep keys that are valid columns
        filtered_data = {k: v for k, v in task_data.items() if k in available_columns}
        return cls(**filtered_data)

    def convert_to_link(self) -> Self:
        """
        Fields to reset when converting to a linked task.
        """
        if self.origin_type == TaskOriginType.link:
            return self  # Already a link, no changes needed

        self.origin_type = TaskOriginType.link
        self.params = None
        self.inputs = None
        self.schemas = None
        self.result = None
        return self

    def is_json_field(self, field: str) -> bool:
        column = self.__class__.__table__.columns.get(field)
        if column is not None and isinstance(column.type, JSON):
            return True
        return False

    def __repr__(self) -> str:
        return f"<TaskModel(id='{self.id}', name='{self.name}', status='{self.status}')>"


class SchemaMigration(Base):
    """
    Schema migration history tracking table

    Records which migrations have been applied and when, including the apflow version
    at the time of application. This enables tracking upgrade history and diagnosing
    version-related issues.
    """

    __tablename__ = "apflow_schema_migrations"  # Fixed table name (not configurable)

    # === Migration Identity ===
    id: Mapped[str] = mapped_column(
        String(100), primary_key=True, index=True, nullable=False
    )  # Migration ID (filename without .py, e.g., "001_add_task_tree_fields")

    # === Migration Info ===
    description: Mapped[str] = mapped_column(Text, nullable=False)  # What this migration does

    # === Version Tracking ===
    apflow_version: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # APFlow version when migration was applied (e.g., "0.2.0")

    # === Timestamps ===
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )

    def __repr__(self) -> str:
        return f"<SchemaMigration(id='{self.id}', apflow_version='{self.apflow_version}')>"


# === Distributed Execution Models ===


class DistributedNode(Base):
    """Registered cluster node with health tracking."""

    __tablename__ = "apflow_distributed_nodes"

    node_id: Mapped[str] = mapped_column(String(100), primary_key=True, nullable=False)
    executor_types: Mapped[Any] = mapped_column(JSON, nullable=False)
    capabilities: Mapped[Any] = mapped_column(JSON, default=dict, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # healthy, stale, dead
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )

    def __repr__(self) -> str:
        return f"<DistributedNode(node_id='{self.node_id}', status='{self.status}')>"


class TaskLease(Base):
    """Active task lease binding a task to a worker node."""

    __tablename__ = "apflow_task_leases"

    task_id: Mapped[str] = mapped_column(
        String(100), ForeignKey(f"{TASK_TABLE_NAME}.id"), primary_key=True, nullable=False
    )
    node_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("apflow_distributed_nodes.node_id"),
        nullable=False,
    )
    lease_token: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    attempt_id: Mapped[int] = mapped_column(Integer, default=0, nullable=True)

    def __repr__(self) -> str:
        return f"<TaskLease(task_id='{self.task_id}', node_id='{self.node_id}')>"


class ExecutionIdempotency(Base):
    """Idempotency tracking for task execution attempts."""

    __tablename__ = "apflow_execution_idempotency"

    task_id: Mapped[str] = mapped_column(String(100), primary_key=True, nullable=False)
    attempt_id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    result: Mapped[Any] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # pending, completed, failed
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )

    def __repr__(self) -> str:
        return f"<ExecutionIdempotency(task_id='{self.task_id}', " f"attempt_id={self.attempt_id})>"


class ClusterLeader(Base):
    """Singleton leader election record."""

    __tablename__ = "apflow_cluster_leader"

    leader_id: Mapped[str] = mapped_column(
        String(100), primary_key=True, default="singleton", nullable=False
    )
    node_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("apflow_distributed_nodes.node_id"),
        nullable=False,
    )
    lease_token: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )

    def __repr__(self) -> str:
        return f"<ClusterLeader(leader_id='{self.leader_id}', node_id='{self.node_id}')>"


class TaskEvent(Base):
    """Distributed-worker audit log — NOT a general task/run history table.

    Scope and intent (read before reusing this table):
    - Purpose: append-only audit trail of the DISTRIBUTED execution lifecycle
      (which node assigned / started / completed / failed / reassigned a task).
      Introduced by migration ``003_add_distributed_support`` purely for
      observability and debugging of multi-node task processing.
    - Writers: ONLY ``core/distributed/`` — ``worker.py`` (task_assigned,
      task_started, task_completed, task_failed) and ``lease_manager.py``
      (task_reassigned). Written exclusively via ``emit_task_event()``, whose
      ``VALID_EVENT_TYPES`` whitelist is closed and raises on unknown types.
    - Readers: NONE in production code. Nothing branches on these rows and no
      API/CLI/module surfaces them; only tests query the table to assert a row
      was written. This is a write-only sink.
    - The in-process scheduler and in-process TaskManager execution paths do
      NOT write here — a task run outside the distributed worker produces no
      TaskEvent rows.

    Do NOT overload this table for per-run history (e.g. scheduler run logs,
    per-fire token usage, result snapshots). ``node_id`` is distributed-specific
    and meaningless for in-process runs, and there is no read side to build on.
    Schedule/execution history belongs in a dedicated ``task_run_history`` table
    with its own schema (run_number, status, token_usage, duration, timestamp).
    """

    __tablename__ = "apflow_task_events"

    event_id: Mapped[str] = mapped_column(
        String(100), primary_key=True, default=lambda: str(uuid.uuid4()), nullable=False
    )
    task_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey(f"{TASK_TABLE_NAME}.id"),
        index=True,
        nullable=False,
    )
    # Closed whitelist enforced by emit_task_event's VALID_EVENT_TYPES:
    # task_assigned, task_started, task_completed, task_failed, task_reassigned.
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    node_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )  # Distributed worker node; null for non-distributed
    details: Mapped[Any] = mapped_column(
        JSON, default=dict, nullable=True
    )  # Event-specific payload (e.g. {"error": ...} on failure)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<TaskEvent(event_id='{self.event_id}', "
            f"task_id='{self.task_id}', event_type='{self.event_type}')>"
        )


class TaskCheckpointModel(Base):
    """Checkpoint storage for durable task execution (F-003)."""

    __tablename__ = "task_checkpoints"

    id: Mapped[str] = mapped_column(
        String(255), primary_key=True, default=lambda: str(uuid.uuid4()), nullable=False
    )
    task_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey(f"{TASK_TABLE_NAME}.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    checkpoint_data: Mapped[str] = mapped_column(Text, nullable=False)
    step_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )

    def __repr__(self) -> str:
        return f"<TaskCheckpoint(id='{self.id}', task_id='{self.task_id}')>"


# All supported ORM writers share the archive invariant.
register_storage_invariants(TaskModel, TaskOriginType.archive, WorkflowRun)
