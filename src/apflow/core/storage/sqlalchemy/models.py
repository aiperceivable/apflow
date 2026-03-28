"""
SQLAlchemy models for task storage
"""

from sqlalchemy import (
    Column,
    String,
    Integer,
    DateTime,
    JSON,
    Text,
    Boolean,
    Numeric,
    ForeignKey,
)
from sqlalchemy.sql import func
from sqlalchemy.orm import declarative_base
from typing import Dict, Any, Optional, TypeVar, Type
from enum import auto, StrEnum
import uuid
import os
from datetime import timezone

Base = declarative_base()

# Type variable for TaskModel subclasses
TaskModelType = TypeVar("TaskModelType", bound="TaskModel")

# Table name configuration - supports environment variable override
# Default: "apflow_tasks" (apflow tasks)
# Can be overridden via APFLOW_TASK_TABLE_NAME environment variable
TASK_TABLE_NAME = os.getenv("APFLOW_TASK_TABLE_NAME", "apflow_tasks")


class TaskOriginType(StrEnum):
    create = auto()  # Task created freshly
    link = auto()  # Task linked from another
    copy = auto()  # Task copied from another (can be modified)
    archive = auto()  # Task archived from another (can not be modified)


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

    # === Task Definition Identity ===
    id = Column(
        String(255), primary_key=True, index=True, default=lambda: str(uuid.uuid4())
    )  # Task definition ID (maps to A2A Task.context_id)

    # === Task Tree Structure (TaskManager) ===
    parent_id = Column(
        String(255), nullable=True, index=True
    )  # Parent task ID (for task tree hierarchy)
    task_tree_id = Column(
        String(255), nullable=True, index=True
    )  # Task tree identifier (for grouping/querying across trees)

    # === User Identification (Optional, Multi-user Support) ===
    user_id = Column(
        String(255), nullable=True, index=True
    )  # User ID (optional, for multi-user scenarios)

    # === Task Basic Information ===
    name = Column(String(100), nullable=False, index=True)  # Task name/method identifier
    status = Column(
        String(50), default="pending", index=True
    )  # Task status: pending, in_progress, completed, failed, cancelled

    # === Task Orchestration (TaskManager) ===
    priority = Column(
        Integer, default=2
    )  # Priority level: 0=urgent (highest), 1=high, 2=normal (default), 3=low (lowest). ASC order: smaller numbers execute first.
    dependencies = Column(
        JSON, nullable=True
    )  # Task dependencies: [{"id": "uuid", "required": true}]

    # === Task Data ===
    inputs = Column(
        JSON, nullable=True
    )  # Execution-time input parameters for executor.execute(inputs)
    params = Column(
        JSON, nullable=True
    )  # Executor initialization parameters for executor.__init__(**params)
    result = Column(
        JSON, nullable=True
    )  # Latest execution result (extracted from A2A Task.artifacts)
    error = Column(Text, nullable=True)  # Error message (extracted from A2A TaskStatus.message)
    schemas = Column(JSON, nullable=True)  # Validation schemas (input_schema, output_schema)

    # Note: A2A Protocol execution fields (artifacts, history, kind, metadata) are NOT stored here.
    # They are managed by A2A Protocol TaskStore as execution instances.

    # === Task Progress ===
    progress = Column(Numeric(3, 2), default=0.0)  # Progress as decimal (0.00 to 1.00)

    # === Timestamps ===
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # === Auxiliary Fields ===
    has_children = Column(Boolean, default=False)  # UI/performance optimization flag

    # === Task Origin/Reference Fields ===
    original_task_id = Column(
        String(255), nullable=True, index=True
    )  # Source task ID (for copy/reference/link)
    origin_type = Column(
        String(50), nullable=True, default=None, index=True
    )  # Origin kind: TaskOriginType
    has_references = Column(
        Boolean, default=False, index=True
    )  # Whether this task is referenced/copied by others

    # === Scheduling Configuration ===
    schedule_type = Column(
        String(20), nullable=True, index=True
    )  # Schedule type: ScheduleType (once, interval, cron, daily, weekly, monthly)
    schedule_expression = Column(
        String(100), nullable=True
    )  # Schedule expression (format depends on schedule_type)
    schedule_enabled = Column(
        Boolean, default=False, index=True
    )  # Whether scheduling is enabled for this task

    # === Schedule Boundaries ===
    schedule_start_at = Column(
        DateTime(timezone=True), nullable=True
    )  # Earliest time the schedule can trigger
    schedule_end_at = Column(
        DateTime(timezone=True), nullable=True
    )  # Latest time the schedule can trigger (after this, schedule is disabled)

    # === Schedule State ===
    next_run_at = Column(
        DateTime(timezone=True), nullable=True, index=True
    )  # Next scheduled execution time (calculated field)
    last_run_at = Column(
        DateTime(timezone=True), nullable=True
    )  # Last time this scheduled task was executed

    # === Execution Control ===
    max_runs = Column(Integer, nullable=True)  # Maximum number of scheduled runs (null = unlimited)
    run_count = Column(Integer, default=0)  # Number of times this scheduled task has been executed

    # === Distributed Execution Fields (nullable, backward compatible) ===
    lease_id = Column(String(100), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    placement_constraints = Column(JSON, nullable=True)
    attempt_id = Column(Integer, default=0)
    idempotency_key = Column(String(255), nullable=True)
    last_assigned_node = Column(String(100), nullable=True)

    # === Durability Fields (F-003) ===
    checkpoint_at = Column(DateTime(timezone=True), nullable=True)
    resume_from = Column(String(255), nullable=True)
    attempt_count = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)
    backoff_strategy = Column(String(20), default="exponential")
    backoff_base_seconds = Column(Numeric(10, 2), default=1.0)

    # === Cost Governance Fields (F-004) ===
    token_usage = Column(JSON, nullable=True)
    token_budget = Column(Integer, nullable=True)
    estimated_cost_usd = Column(Numeric(12, 6), nullable=True)
    actual_cost_usd = Column(Numeric(12, 6), nullable=True)
    cost_policy = Column(String(100), nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        """Convert model to dictionary"""
        return {
            # Task definition identity
            "id": self.id,
            # Task tree structure
            "parent_id": self.parent_id,
            "task_tree_id": self.task_tree_id,
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
            "schemas": self.schemas,
            # Task progress
            "progress": float(self.progress) if self.progress is not None else 0.0,
            # Timestamps
            "created_at": self.created_at,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
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
            "estimated_cost_usd": (
                float(self.estimated_cost_usd) if self.estimated_cost_usd else None
            ),
            "actual_cost_usd": (
                float(self.actual_cost_usd) if self.actual_cost_usd else None
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
        # Schedule datetime fields
        data["schedule_start_at"] = (
            self.schedule_start_at.isoformat() if self.schedule_start_at else None
        )
        data["schedule_end_at"] = self.schedule_end_at.isoformat() if self.schedule_end_at else None
        data["next_run_at"] = self.next_run_at.isoformat() if self.next_run_at else None
        data["last_run_at"] = self.last_run_at.isoformat() if self.last_run_at else None
        return data

    def copy(self, override: Optional[Dict[str, Any]] = None) -> TaskModelType:
        """
        Return a new instance of this TaskModel (or subclass), optionally overriding fields.
        """
        data = self.to_dict()
        if override:
            data.update(override)
        return self.__class__(**data)

    def update_from_dict(self, data: Dict[str, Any]) -> TaskModelType:
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
            "parent_id": None,
            "priority": 1,
            "dependencies": [],
            "inputs": {},
            "schemas": {},
            "params": {},
            "status": "pending",
            "progress": 0.0,
            "has_children": False,
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
            "estimated_cost_usd": None,
            "actual_cost_usd": None,
            "cost_policy": None,
        }

    @classmethod
    def create(cls: Type[TaskModelType], data: Dict[str, Any]) -> TaskModelType:
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

    def convert_to_link(self) -> TaskModelType:
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

    def __repr__(self):
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
    id = Column(
        String(100), primary_key=True, index=True
    )  # Migration ID (filename without .py, e.g., "001_add_task_tree_fields")

    # === Migration Info ===
    description = Column(Text, nullable=False)  # What this migration does

    # === Version Tracking ===
    apflow_version = Column(
        String(50), nullable=False
    )  # APFlow version when migration was applied (e.g., "0.2.0")

    # === Timestamps ===
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<SchemaMigration(id='{self.id}', apflow_version='{self.apflow_version}')>"


# === Distributed Execution Models ===


class DistributedNode(Base):
    """Registered cluster node with health tracking."""

    __tablename__ = "apflow_distributed_nodes"

    node_id = Column(String(100), primary_key=True)
    executor_types = Column(JSON, nullable=False)
    capabilities = Column(JSON, default=dict)
    status = Column(String(20), nullable=False)  # healthy, stale, dead
    heartbeat_at = Column(DateTime(timezone=True), nullable=False)
    registered_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<DistributedNode(node_id='{self.node_id}', status='{self.status}')>"


class TaskLease(Base):
    """Active task lease binding a task to a worker node."""

    __tablename__ = "apflow_task_leases"

    task_id = Column(
        String(100),
        ForeignKey(f"{TASK_TABLE_NAME}.id"),
        primary_key=True,
    )
    node_id = Column(
        String(100),
        ForeignKey("apflow_distributed_nodes.node_id"),
        nullable=False,
    )
    lease_token = Column(String(100), unique=True, index=True, nullable=False)
    acquired_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), index=True, nullable=False)
    attempt_id = Column(Integer, default=0)

    def __repr__(self) -> str:
        return f"<TaskLease(task_id='{self.task_id}', node_id='{self.node_id}')>"


class ExecutionIdempotency(Base):
    """Idempotency tracking for task execution attempts."""

    __tablename__ = "apflow_execution_idempotency"

    task_id = Column(String(100), primary_key=True)
    attempt_id = Column(Integer, primary_key=True)
    idempotency_key = Column(String(255), unique=True, index=True, nullable=False)
    result = Column(JSON, nullable=True)
    status = Column(String(20), nullable=False)  # pending, completed, failed
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ExecutionIdempotency(task_id='{self.task_id}', " f"attempt_id={self.attempt_id})>"


class ClusterLeader(Base):
    """Singleton leader election record."""

    __tablename__ = "apflow_cluster_leader"

    leader_id = Column(String(100), primary_key=True, default="singleton")
    node_id = Column(
        String(100),
        ForeignKey("apflow_distributed_nodes.node_id"),
        nullable=False,
    )
    lease_token = Column(String(100), unique=True, nullable=False)
    acquired_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), index=True, nullable=False)

    def __repr__(self) -> str:
        return f"<ClusterLeader(leader_id='{self.leader_id}', node_id='{self.node_id}')>"


class TaskEvent(Base):
    """Audit log for task lifecycle events."""

    __tablename__ = "apflow_task_events"

    event_id = Column(
        String(100),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    task_id = Column(
        String(100),
        ForeignKey(f"{TASK_TABLE_NAME}.id"),
        index=True,
        nullable=False,
    )
    event_type = Column(String(50), nullable=False)
    node_id = Column(String(100), nullable=True)
    details = Column(JSON, default=dict)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    def __repr__(self) -> str:
        return (
            f"<TaskEvent(event_id='{self.event_id}', "
            f"task_id='{self.task_id}', event_type='{self.event_type}')>"
        )


class TaskCheckpointModel(Base):
    """Checkpoint storage for durable task execution (F-003)."""

    __tablename__ = "task_checkpoints"

    id = Column(String(255), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(
        String(255),
        ForeignKey(f"{TASK_TABLE_NAME}.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    checkpoint_data = Column(Text, nullable=False)
    step_name = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<TaskCheckpoint(id='{self.id}', task_id='{self.task_id}')>"
