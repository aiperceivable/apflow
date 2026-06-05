"""Node registry service for distributed cluster management.

Manages registration, heartbeat, and health detection of cluster nodes.
"""

from datetime import timedelta

from sqlalchemy.orm import sessionmaker

from apflow.core.distributed.config import DistributedConfig, utcnow as _utcnow
from apflow.core.distributed.types import NodeCapabilities
from apflow.core.storage.sqlalchemy.models import DistributedNode
from apflow.logger import get_logger

logger = get_logger(__name__)


class NodeNotFoundError(Exception):
    """Raised when a referenced node does not exist."""


class NodeRegistry:
    """Manages cluster node lifecycle: registration, heartbeat, and health detection."""

    def __init__(self, session_factory: sessionmaker, config: DistributedConfig) -> None:
        self._session_factory = session_factory
        self._config = config

    def register_node(
        self,
        node_id: str,
        executor_types: list[str],
        capabilities: NodeCapabilities | None = None,
    ) -> DistributedNode:
        """Register a new node or update an existing one.

        Uses merge() for atomic upsert semantics.
        """
        if not node_id:
            raise ValueError("node_id must not be empty")

        with self._session_factory() as session:
            now = _utcnow()
            node = DistributedNode(
                node_id=node_id,
                executor_types=executor_types,
                capabilities=capabilities or {},
                status="healthy",
                heartbeat_at=now,
            )
            merged = session.merge(node)
            session.commit()
            logger.info("Registered/updated node: %s", node_id)
            return merged

    def deregister_node(self, node_id: str) -> None:
        """Remove a node from the registry."""
        if not node_id:
            raise ValueError("node_id must not be empty")

        with self._session_factory() as session:
            node = session.get(DistributedNode, node_id)
            if node is None:
                raise NodeNotFoundError(f"Node '{node_id}' not found")
            session.delete(node)
            session.commit()
            logger.info("Deregistered node: %s", node_id)

    def heartbeat(self, node_id: str) -> None:
        """Update heartbeat timestamp and mark node as healthy."""
        if not node_id:
            raise ValueError("node_id must not be empty")

        with self._session_factory() as session:
            node = session.get(DistributedNode, node_id)
            if node is None:
                raise NodeNotFoundError(f"Node '{node_id}' not found")
            node.heartbeat_at = _utcnow()
            node.status = "healthy"
            session.commit()
            logger.info("Heartbeat received from node: %s", node_id)

    def detect_stale_nodes(self) -> list[DistributedNode]:
        """Detect and mark nodes as stale based on heartbeat threshold.

        A node is stale if its heartbeat is older than node_stale_threshold_seconds
        but not yet past node_dead_threshold_seconds.
        """
        with self._session_factory() as session:
            now = _utcnow()
            stale_cutoff = now - timedelta(seconds=self._config.node_stale_threshold_seconds)
            dead_cutoff = now - timedelta(seconds=self._config.node_dead_threshold_seconds)

            stale_nodes = (
                session.query(DistributedNode)
                .filter(
                    DistributedNode.heartbeat_at <= stale_cutoff,
                    DistributedNode.heartbeat_at > dead_cutoff,
                    DistributedNode.status != "dead",
                )
                .all()
            )

            for node in stale_nodes:
                node.status = "stale"
                logger.info("Node marked stale: %s", node.node_id)

            session.commit()
            # Detach while attributes are loaded so callers can read the returned
            # nodes after the session closes (no DetachedInstanceError).
            session.expunge_all()
            return stale_nodes

    def detect_dead_nodes(self) -> list[DistributedNode]:
        """Detect and mark nodes as dead based on heartbeat threshold.

        A node is dead if its heartbeat is older than node_dead_threshold_seconds.
        """
        with self._session_factory() as session:
            now = _utcnow()
            dead_cutoff = now - timedelta(seconds=self._config.node_dead_threshold_seconds)

            dead_nodes = (
                session.query(DistributedNode)
                .filter(
                    DistributedNode.heartbeat_at <= dead_cutoff,
                    DistributedNode.status != "dead",
                )
                .all()
            )

            for node in dead_nodes:
                node.status = "dead"
                logger.info("Node marked dead: %s", node.node_id)

            session.commit()
            session.expunge_all()
            return dead_nodes

    def get_healthy_nodes(
        self,
        executor_types: list[str] | None = None,
        capabilities: NodeCapabilities | None = None,
    ) -> list[DistributedNode]:
        """Query healthy nodes, optionally filtered by executor types and capabilities."""
        with self._session_factory() as session:
            query = session.query(DistributedNode).filter(
                DistributedNode.status == "healthy",
            )
            nodes = query.all()
            # Detach before filtering/returning so reads survive session close.
            session.expunge_all()

            if executor_types is not None:
                nodes = _filter_by_executor_types(nodes, executor_types)

            if capabilities is not None:
                nodes = _filter_by_capabilities(nodes, capabilities)

            return nodes


def _filter_by_executor_types(
    nodes: list[DistributedNode],
    required_types: list[str],
) -> list[DistributedNode]:
    """Return nodes whose executor_types contain all required types."""
    result: list[DistributedNode] = []
    for node in nodes:
        node_types = node.executor_types or []
        if all(t in node_types for t in required_types):
            result.append(node)
    return result


def _filter_by_capabilities(
    nodes: list[DistributedNode],
    required_capabilities: NodeCapabilities,
) -> list[DistributedNode]:
    """Return nodes whose capabilities match all required key-value pairs."""
    result: list[DistributedNode] = []
    for node in nodes:
        node_caps = node.capabilities or {}
        if all(node_caps.get(k) == v for k, v in required_capabilities.items()):
            result.append(node)
    return result
