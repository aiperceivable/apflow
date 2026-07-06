"""
Distributed cluster configuration for apflow

Provides DistributedConfig dataclass for managing cluster settings
including node identity, lease management, and heartbeat configuration.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Union

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

VALID_NODE_ROLES = ("auto", "leader", "worker", "observer")


def _int_env(key: str, default: str) -> int:
    """Read an int from environment, falling back to default on parse error."""
    value = os.getenv(key, default)
    try:
        return int(value)
    except ValueError:
        return int(default)


# All datetime values in the distributed module use timezone-aware UTC
# (datetime.now(timezone.utc)). The as_utc() helper normalizes naive datetimes
# from databases (like SQLite) that strip timezone info on read-back.
def utcnow() -> datetime:
    """Return current UTC time as a timezone-aware datetime.

    Uses timezone-aware datetimes to match model columns that specify
    ``DateTime(timezone=True)``.
    """
    return datetime.now(timezone.utc)


def as_utc(dt: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC.

    SQLite does not preserve timezone info for ``DateTime(timezone=True)``
    columns, so values read back are offset-naive.  This helper ensures
    consistent comparison with :func:`utcnow` results by assuming naive
    datetimes are already in UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_postgresql(session: Union[Session, AsyncSession]) -> bool:
    """Check if the session is bound to a PostgreSQL database."""
    bind = session.get_bind()
    return bind.dialect.name == "postgresql"


@dataclass
class DistributedConfig:
    """Configuration for distributed task orchestration.

    Controls cluster behavior including node identity, lease durations,
    heartbeat intervals, and parallel task limits.
    """

    enabled: bool = False
    node_id: str | None = None
    node_role: str = "auto"  # auto | leader | worker | observer

    # Leader election
    leader_lease_seconds: int = 30
    leader_renew_seconds: int = 10

    # Task lease management
    lease_duration_seconds: int = 30
    lease_cleanup_interval_seconds: int = 10

    # Worker polling
    poll_interval_seconds: int = 5
    max_parallel_tasks_per_node: int = 4

    # Leader-side scheduling: when enabled, the leader runs a dispatch-only
    # scheduler that turns due schedules into pending run instances for workers
    # to lease and execute. Exactly one node (the leader) dispatches.
    scheduling_enabled: bool = False
    scheduler_poll_interval_seconds: int = 30

    # Node health monitoring
    heartbeat_interval_seconds: int = 10
    node_stale_threshold_seconds: int = 30
    node_dead_threshold_seconds: int = 120

    @classmethod
    def from_env(cls) -> DistributedConfig:
        """Load configuration from environment variables."""
        return cls(
            enabled=os.getenv("APFLOW_CLUSTER_ENABLED", "false").lower() == "true",
            node_id=os.getenv("APFLOW_NODE_ID"),
            node_role=os.getenv("APFLOW_NODE_ROLE", "auto"),
            leader_lease_seconds=_int_env("APFLOW_LEADER_LEASE", "30"),
            leader_renew_seconds=_int_env("APFLOW_LEADER_RENEW", "10"),
            lease_duration_seconds=_int_env("APFLOW_LEASE_DURATION", "30"),
            lease_cleanup_interval_seconds=_int_env("APFLOW_LEASE_CLEANUP_INTERVAL", "10"),
            poll_interval_seconds=_int_env("APFLOW_POLL_INTERVAL", "5"),
            max_parallel_tasks_per_node=_int_env("APFLOW_MAX_PARALLEL_TASKS", "4"),
            scheduling_enabled=os.getenv("APFLOW_CLUSTER_SCHEDULING", "false").lower() == "true",
            scheduler_poll_interval_seconds=_int_env("APFLOW_SCHEDULER_POLL_INTERVAL", "30"),
            heartbeat_interval_seconds=_int_env("APFLOW_HEARTBEAT_INTERVAL", "10"),
            node_stale_threshold_seconds=_int_env("APFLOW_NODE_STALE_THRESHOLD", "30"),
            node_dead_threshold_seconds=_int_env("APFLOW_NODE_DEAD_THRESHOLD", "120"),
        )

    def validate_and_initialize(self) -> None:
        """Validate configuration values and auto-generate node_id if needed.

        Raises:
            ValueError: If any configuration value is invalid.
        """
        if self.node_role not in VALID_NODE_ROLES:
            raise ValueError(f"node_role must be one of {VALID_NODE_ROLES}, got '{self.node_role}'")

        positive_fields = [
            "leader_lease_seconds",
            "leader_renew_seconds",
            "lease_duration_seconds",
            "lease_cleanup_interval_seconds",
            "poll_interval_seconds",
            "max_parallel_tasks_per_node",
            "heartbeat_interval_seconds",
            "node_stale_threshold_seconds",
            "node_dead_threshold_seconds",
        ]
        for field_name in positive_fields:
            value = getattr(self, field_name)
            if value <= 0:
                raise ValueError(f"{field_name} must be positive, got {value}")

        # Cross-field invariant: the renewal must fire before the lease expires,
        # otherwise the leader silently loses and re-acquires leadership every
        # cycle (flapping), repeatedly restarting the worker runtime.
        if self.leader_renew_seconds >= self.leader_lease_seconds:
            raise ValueError(
                "leader_renew_seconds must be less than leader_lease_seconds "
                f"(got renew={self.leader_renew_seconds}, lease={self.leader_lease_seconds})"
            )

        # Auto-generate node_id if enabled and not set
        if self.enabled and self.node_id is None:
            self.node_id = f"node-{uuid.uuid4().hex[:12]}"
