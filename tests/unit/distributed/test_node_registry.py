"""Tests for NodeRegistry distributed service."""

import pytest
from datetime import datetime, timedelta, timezone

from apflow.core.distributed.config import as_utc
from apflow.core.distributed.node_registry import NodeRegistry, NodeNotFoundError
from apflow.core.storage.sqlalchemy.models import DistributedNode


class TestNodeRegistry:
    """Tests for NodeRegistry service methods."""

    def test_register_node_creates_entry(self, session_factory, config, session):
        """Registering a new node creates a DB entry with healthy status."""
        registry = NodeRegistry(session_factory, config)

        result = registry.register_node(
            node_id="node-alpha",
            executor_types=["a2a", "mcp"],
            capabilities={"gpu": True},
        )

        assert result.node_id == "node-alpha"
        assert result.executor_types == ["a2a", "mcp"]
        assert result.capabilities == {"gpu": True}
        assert result.status == "healthy"

        # Verify persisted in DB
        persisted = session.get(DistributedNode, "node-alpha")
        assert persisted is not None
        assert persisted.status == "healthy"

    def test_register_node_idempotent(self, session_factory, config, session):
        """Re-registering the same node_id updates rather than duplicates."""
        registry = NodeRegistry(session_factory, config)

        registry.register_node(
            node_id="node-beta",
            executor_types=["a2a"],
            capabilities={"gpu": False},
        )
        updated = registry.register_node(
            node_id="node-beta",
            executor_types=["mcp"],
            capabilities={"gpu": True},
        )

        assert updated.executor_types == ["mcp"]
        assert updated.capabilities == {"gpu": True}

        # Only one row in DB
        all_nodes = session.query(DistributedNode).filter_by(node_id="node-beta").all()
        assert len(all_nodes) == 1

    def test_heartbeat_updates_timestamp(self, session_factory, config, sample_node, session):
        """Heartbeat updates the heartbeat_at field and resets status to healthy."""
        registry = NodeRegistry(session_factory, config)

        old_heartbeat = sample_node.heartbeat_at
        registry.heartbeat("worker-1")

        session.refresh(sample_node)
        assert as_utc(sample_node.heartbeat_at) >= as_utc(old_heartbeat)
        assert sample_node.status == "healthy"

    def test_heartbeat_nonexistent_node_raises(self, session_factory, config):
        """Heartbeat for a non-existent node raises NodeNotFoundError."""
        registry = NodeRegistry(session_factory, config)

        with pytest.raises(NodeNotFoundError, match="not found"):
            registry.heartbeat("nonexistent-node")

    def test_detect_stale_nodes(self, session_factory, config, session):
        """Nodes with heartbeat older than stale threshold are marked stale."""
        registry = NodeRegistry(session_factory, config)

        now = datetime.now(timezone.utc)
        stale_time = now - timedelta(seconds=config.node_stale_threshold_seconds + 5)

        stale_node = DistributedNode(
            node_id="stale-worker",
            executor_types=["a2a"],
            capabilities={},
            status="healthy",
            heartbeat_at=stale_time,
        )
        session.add(stale_node)
        session.commit()

        result = registry.detect_stale_nodes()

        assert len(result) == 1
        assert result[0].node_id == "stale-worker"
        assert result[0].status == "stale"

    def test_detect_dead_nodes(self, session_factory, config, session):
        """Nodes with heartbeat older than dead threshold are marked dead."""
        registry = NodeRegistry(session_factory, config)

        now = datetime.now(timezone.utc)
        dead_time = now - timedelta(seconds=config.node_dead_threshold_seconds + 10)

        dead_node = DistributedNode(
            node_id="dead-worker",
            executor_types=["a2a"],
            capabilities={},
            status="healthy",
            heartbeat_at=dead_time,
        )
        session.add(dead_node)
        session.commit()

        result = registry.detect_dead_nodes()

        assert len(result) == 1
        assert result[0].node_id == "dead-worker"
        assert result[0].status == "dead"

    def test_deregister_node_removes_entry(self, session_factory, config, sample_node, session):
        """Deregistering a node removes it from the database."""
        registry = NodeRegistry(session_factory, config)

        registry.deregister_node("worker-1")

        session.expire_all()
        persisted = session.get(DistributedNode, "worker-1")
        assert persisted is None

    def test_deregister_nonexistent_node_raises(self, session_factory, config):
        """Deregistering a non-existent node raises NodeNotFoundError."""
        registry = NodeRegistry(session_factory, config)

        with pytest.raises(NodeNotFoundError, match="not found"):
            registry.deregister_node("ghost-node")

    def test_get_healthy_nodes_filter_by_executor_types(self, session_factory, config, session):
        """get_healthy_nodes filters by executor_types correctly."""
        registry = NodeRegistry(session_factory, config)

        node_a2a = DistributedNode(
            node_id="worker-a2a",
            executor_types=["a2a"],
            capabilities={},
            status="healthy",
            heartbeat_at=datetime.now(timezone.utc),
        )
        node_mcp = DistributedNode(
            node_id="worker-mcp",
            executor_types=["mcp"],
            capabilities={},
            status="healthy",
            heartbeat_at=datetime.now(timezone.utc),
        )
        node_both = DistributedNode(
            node_id="worker-both",
            executor_types=["a2a", "mcp"],
            capabilities={},
            status="healthy",
            heartbeat_at=datetime.now(timezone.utc),
        )
        session.add_all([node_a2a, node_mcp, node_both])
        session.commit()

        result = registry.get_healthy_nodes(executor_types=["a2a"])

        node_ids = [n.node_id for n in result]
        assert "worker-a2a" in node_ids
        assert "worker-both" in node_ids
        assert "worker-mcp" not in node_ids


class TestNodeRegistryDetaches:
    """Returned nodes are detached (expunged) so reads survive session close."""

    def test_get_healthy_nodes_expunges(self):
        from unittest.mock import MagicMock

        from apflow.core.distributed.config import DistributedConfig

        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        session.query.return_value.filter.return_value.all.return_value = []
        factory = MagicMock(return_value=session)

        registry = NodeRegistry(factory, DistributedConfig(node_id="n"))
        registry.get_healthy_nodes()

        session.expunge_all.assert_called_once()
