"""Tests for DistributedConfig dataclass."""

import os
from unittest.mock import MagicMock, patch

import pytest


class TestDistributedConfigDefaults:
    """Test DistributedConfig default values."""

    def test_defaults(self) -> None:
        """DistributedConfig has correct defaults."""
        from apflow.core.distributed.config import DistributedConfig

        config = DistributedConfig()
        assert config.enabled is False
        assert config.node_id is None
        assert config.node_role == "auto"
        assert config.leader_lease_seconds == 30
        assert config.leader_renew_seconds == 10
        assert config.lease_duration_seconds == 30
        assert config.lease_cleanup_interval_seconds == 10
        assert config.poll_interval_seconds == 5
        assert config.max_parallel_tasks_per_node == 4
        assert config.heartbeat_interval_seconds == 10
        assert config.node_stale_threshold_seconds == 30
        assert config.node_dead_threshold_seconds == 120

    def test_custom_values(self) -> None:
        """DistributedConfig accepts custom values."""
        from apflow.core.distributed.config import DistributedConfig

        config = DistributedConfig(
            enabled=True,
            node_id="node-1",
            node_role="leader",
            max_parallel_tasks_per_node=8,
        )
        assert config.enabled is True
        assert config.node_id == "node-1"
        assert config.node_role == "leader"
        assert config.max_parallel_tasks_per_node == 8


class TestDistributedConfigFromEnv:
    """Test DistributedConfig.from_env()."""

    def test_from_env_disabled_by_default(self) -> None:
        """from_env() returns disabled config when env vars not set."""
        from apflow.core.distributed.config import DistributedConfig

        with patch.dict(os.environ, {}, clear=True):
            config = DistributedConfig.from_env()
            assert config.enabled is False

    def test_from_env_enabled(self) -> None:
        """from_env() reads APFLOW_CLUSTER_ENABLED."""
        from apflow.core.distributed.config import DistributedConfig

        env = {
            "APFLOW_CLUSTER_ENABLED": "true",
            "APFLOW_NODE_ID": "worker-42",
            "APFLOW_NODE_ROLE": "worker",
        }
        with patch.dict(os.environ, env, clear=True):
            config = DistributedConfig.from_env()
            assert config.enabled is True
            assert config.node_id == "worker-42"
            assert config.node_role == "worker"

    def test_scheduling_disabled_by_default(self) -> None:
        """Leader-side scheduling is off unless explicitly enabled."""
        from apflow.core.distributed.config import DistributedConfig

        assert DistributedConfig().scheduling_enabled is False
        with patch.dict(os.environ, {}, clear=True):
            assert DistributedConfig.from_env().scheduling_enabled is False

    def test_from_env_scheduling_enabled(self) -> None:
        """from_env() reads APFLOW_CLUSTER_SCHEDULING and the scheduler poll interval."""
        from apflow.core.distributed.config import DistributedConfig

        env = {
            "APFLOW_CLUSTER_SCHEDULING": "true",
            "APFLOW_SCHEDULER_POLL_INTERVAL": "15",
        }
        with patch.dict(os.environ, env, clear=True):
            config = DistributedConfig.from_env()
            assert config.scheduling_enabled is True
            assert config.scheduler_poll_interval_seconds == 15

    def test_from_env_numeric_values(self) -> None:
        """from_env() reads numeric configuration."""
        from apflow.core.distributed.config import DistributedConfig

        env = {
            "APFLOW_CLUSTER_ENABLED": "true",
            "APFLOW_MAX_PARALLEL_TASKS": "16",
            "APFLOW_HEARTBEAT_INTERVAL": "5",
        }
        with patch.dict(os.environ, env, clear=True):
            config = DistributedConfig.from_env()
            assert config.max_parallel_tasks_per_node == 16
            assert config.heartbeat_interval_seconds == 5


class TestDistributedConfigValidation:
    """Test DistributedConfig.validate_and_initialize()."""

    def test_valid_roles(self) -> None:
        """validate_and_initialize() accepts valid node roles."""
        from apflow.core.distributed.config import DistributedConfig

        for role in ("auto", "leader", "worker", "observer"):
            config = DistributedConfig(node_role=role)
            config.validate_and_initialize()  # Should not raise

    def test_rejects_invalid_role(self) -> None:
        """validate_and_initialize() raises ValueError for invalid node_role."""
        from apflow.core.distributed.config import DistributedConfig

        config = DistributedConfig(node_role="invalid_role")
        with pytest.raises(ValueError, match="node_role"):
            config.validate_and_initialize()

    def test_rejects_negative_intervals(self) -> None:
        """validate_and_initialize() raises ValueError for non-positive intervals."""
        from apflow.core.distributed.config import DistributedConfig

        config = DistributedConfig(heartbeat_interval_seconds=-1)
        with pytest.raises(ValueError, match="heartbeat_interval_seconds"):
            config.validate_and_initialize()

    def test_rejects_zero_lease_duration(self) -> None:
        """validate_and_initialize() raises ValueError for zero lease duration."""
        from apflow.core.distributed.config import DistributedConfig

        config = DistributedConfig(lease_duration_seconds=0)
        with pytest.raises(ValueError, match="lease_duration_seconds"):
            config.validate_and_initialize()

    def test_enabled_requires_node_id_generated(self) -> None:
        """validate_and_initialize() auto-generates node_id if enabled and node_id is None."""
        from apflow.core.distributed.config import DistributedConfig

        config = DistributedConfig(enabled=True, node_id=None)
        config.validate_and_initialize()
        assert config.node_id is not None
        assert len(config.node_id) > 0


class TestDistributedConfigRegistry:
    """Test config registry integration."""

    def test_get_set_distributed_config(self) -> None:
        """get_distributed_config / set_distributed_config round-trip works."""
        from apflow.core.distributed.config import DistributedConfig

        config = DistributedConfig(enabled=True, node_id="test-node")

        from apflow.core.config.registry import (
            get_distributed_config,
            set_distributed_config,
        )

        set_distributed_config(config)
        retrieved = get_distributed_config()
        assert retrieved.enabled is True
        assert retrieved.node_id == "test-node"

    def test_get_distributed_config_returns_default(self) -> None:
        """get_distributed_config() returns disabled config when not set."""
        from apflow.core.config.registry import (
            _get_registry,
            get_distributed_config,
        )

        # Clear registry state
        registry = _get_registry()
        registry._distributed_config = None

        config = get_distributed_config()
        assert config.enabled is False


class TestIsPostgresql:
    """Test is_postgresql shared helper function."""

    def test_postgresql_dialect_returns_true(self) -> None:
        """is_postgresql returns True for postgresql dialect."""
        from apflow.core.distributed.config import is_postgresql

        session = MagicMock()
        session.get_bind.return_value.dialect.name = "postgresql"
        assert is_postgresql(session) is True

    def test_sqlite_dialect_returns_false(self) -> None:
        """is_postgresql returns False for sqlite dialect."""
        from apflow.core.distributed.config import is_postgresql

        session = MagicMock()
        session.get_bind.return_value.dialect.name = "sqlite"
        assert is_postgresql(session) is False


class TestLeaderTimingValidation:
    """Regression: leader_renew_seconds must be < leader_lease_seconds, else the
    lease expires before renewal fires and the leader flaps silently. (Review W3)
    """

    def test_renew_not_less_than_lease_raises(self) -> None:
        from apflow.core.distributed.config import DistributedConfig

        config = DistributedConfig(
            enabled=True,
            node_id="n1",
            leader_lease_seconds=30,
            leader_renew_seconds=40,
        )
        with pytest.raises(ValueError, match="leader_renew_seconds"):
            config.validate_and_initialize()

    def test_renew_equal_to_lease_raises(self) -> None:
        from apflow.core.distributed.config import DistributedConfig

        config = DistributedConfig(
            enabled=True,
            node_id="n1",
            leader_lease_seconds=30,
            leader_renew_seconds=30,
        )
        with pytest.raises(ValueError, match="leader_renew_seconds"):
            config.validate_and_initialize()

    def test_renew_less_than_lease_ok(self) -> None:
        from apflow.core.distributed.config import DistributedConfig

        config = DistributedConfig(
            enabled=True,
            node_id="n1",
            leader_lease_seconds=30,
            leader_renew_seconds=10,
        )
        config.validate_and_initialize()  # must not raise
