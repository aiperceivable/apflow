"""Tests for distributed SQLAlchemy models."""

from sqlalchemy import inspect


class TestDistributedNodeModel:
    """Test DistributedNode model."""

    def test_distributed_node_model_fields(self) -> None:
        """DistributedNode has required columns."""
        from apflow.core.storage.sqlalchemy.models import DistributedNode

        columns = {c.name for c in DistributedNode.__table__.columns}
        expected = {
            "node_id",
            "executor_types",
            "capabilities",
            "status",
            "heartbeat_at",
            "registered_at",
        }
        assert expected.issubset(columns)

    def test_distributed_node_primary_key(self) -> None:
        """DistributedNode has node_id as primary key."""
        from apflow.core.storage.sqlalchemy.models import DistributedNode

        pk_cols = [c.name for c in inspect(DistributedNode).primary_key]
        assert pk_cols == ["node_id"]

    def test_distributed_node_tablename(self) -> None:
        """DistributedNode uses correct table name."""
        from apflow.core.storage.sqlalchemy.models import DistributedNode

        assert DistributedNode.__tablename__ == "apflow_distributed_nodes"


class TestTaskLeaseModel:
    """Test TaskLease model."""

    def test_task_lease_model_fields(self) -> None:
        """TaskLease has required columns."""
        from apflow.core.storage.sqlalchemy.models import TaskLease

        columns = {c.name for c in TaskLease.__table__.columns}
        expected = {
            "task_id",
            "node_id",
            "lease_token",
            "acquired_at",
            "expires_at",
            "attempt_id",
        }
        assert expected.issubset(columns)

    def test_task_lease_primary_key(self) -> None:
        """TaskLease has task_id as primary key."""
        from apflow.core.storage.sqlalchemy.models import TaskLease

        pk_cols = [c.name for c in inspect(TaskLease).primary_key]
        assert pk_cols == ["task_id"]


class TestExecutionIdempotencyModel:
    """Test ExecutionIdempotency model."""

    def test_execution_idempotency_model_fields(self) -> None:
        """ExecutionIdempotency has required columns."""
        from apflow.core.storage.sqlalchemy.models import ExecutionIdempotency

        columns = {c.name for c in ExecutionIdempotency.__table__.columns}
        expected = {
            "task_id",
            "attempt_id",
            "idempotency_key",
            "result",
            "status",
            "created_at",
        }
        assert expected.issubset(columns)

    def test_execution_idempotency_composite_pk(self) -> None:
        """ExecutionIdempotency has composite PK (task_id, attempt_id)."""
        from apflow.core.storage.sqlalchemy.models import ExecutionIdempotency

        pk_cols = [c.name for c in inspect(ExecutionIdempotency).primary_key]
        assert set(pk_cols) == {"task_id", "attempt_id"}


class TestClusterLeaderModel:
    """Test ClusterLeader model."""

    def test_cluster_leader_model_fields(self) -> None:
        """ClusterLeader has required columns."""
        from apflow.core.storage.sqlalchemy.models import ClusterLeader

        columns = {c.name for c in ClusterLeader.__table__.columns}
        expected = {
            "leader_id",
            "node_id",
            "lease_token",
            "acquired_at",
            "expires_at",
        }
        assert expected.issubset(columns)

    def test_cluster_leader_primary_key(self) -> None:
        """ClusterLeader has leader_id as primary key."""
        from apflow.core.storage.sqlalchemy.models import ClusterLeader

        pk_cols = [c.name for c in inspect(ClusterLeader).primary_key]
        assert pk_cols == ["leader_id"]


class TestTaskEventModel:
    """Test TaskEvent model."""

    def test_task_event_model_fields(self) -> None:
        """TaskEvent has required columns."""
        from apflow.core.storage.sqlalchemy.models import TaskEvent

        columns = {c.name for c in TaskEvent.__table__.columns}
        expected = {
            "event_id",
            "task_id",
            "event_type",
            "node_id",
            "details",
            "timestamp",
        }
        assert expected.issubset(columns)

    def test_task_event_primary_key(self) -> None:
        """TaskEvent has event_id as primary key."""
        from apflow.core.storage.sqlalchemy.models import TaskEvent

        pk_cols = [c.name for c in inspect(TaskEvent).primary_key]
        assert pk_cols == ["event_id"]

    def test_task_event_tablename(self) -> None:
        """TaskEvent uses correct table name."""
        from apflow.core.storage.sqlalchemy.models import TaskEvent

        assert TaskEvent.__tablename__ == "apflow_task_events"


class TestTaskModelDistributedFields:
    """Test TaskModel distributed field extensions."""

    def test_task_model_distributed_fields_exist(self) -> None:
        """TaskModel has all 6 distributed fields."""
        from apflow.core.storage.sqlalchemy.models import TaskModel

        columns = {c.name for c in TaskModel.__table__.columns}
        distributed_fields = {
            "lease_id",
            "lease_expires_at",
            "placement_constraints",
            "attempt_id",
            "idempotency_key",
            "last_assigned_node",
        }
        assert distributed_fields.issubset(columns)

    def test_task_model_distributed_fields_nullable(self) -> None:
        """TaskModel distributed fields are nullable (backward compatible)."""
        from apflow.core.storage.sqlalchemy.models import TaskModel

        nullable_fields = [
            "lease_id",
            "lease_expires_at",
            "placement_constraints",
            "idempotency_key",
            "last_assigned_node",
        ]
        for field_name in nullable_fields:
            col = TaskModel.__table__.columns[field_name]
            assert col.nullable, f"{field_name} should be nullable"

    def test_task_model_to_dict_includes_distributed_fields(self) -> None:
        """TaskModel.to_dict() includes distributed fields."""
        from apflow.core.storage.sqlalchemy.models import TaskModel

        task = TaskModel(id="test-1", name="test")
        data = task.to_dict()
        assert "lease_id" in data
        assert "lease_expires_at" in data
        assert "placement_constraints" in data
        assert "attempt_id" in data
        assert "idempotency_key" in data
        assert "last_assigned_node" in data

    def test_task_model_default_values_includes_distributed_fields(self) -> None:
        """TaskModel.default_values() includes distributed fields."""
        from apflow.core.storage.sqlalchemy.models import TaskModel

        defaults = TaskModel().default_values()
        assert "lease_id" in defaults
        assert defaults["lease_id"] is None
        assert defaults["attempt_id"] == 0
