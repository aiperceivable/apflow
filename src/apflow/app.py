"""
Application bootstrap for apflow.

Provides create_app() factory that initializes the full apflow stack:
session → TaskManager → TaskCreator → TaskRepository → apcore Registry.

Used by CLI entry points (serve, mcp) and can be called programmatically.
"""

from typing import TYPE_CHECKING, Optional, Union

from apflow.core.config_manager import get_config_manager
from apflow.logger import get_logger

if TYPE_CHECKING:
    from apcore import Registry
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session

    from apflow.core.execution.task_creator import TaskCreator
    from apflow.core.execution.task_manager import TaskManager
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

logger = get_logger(__name__)


class ApflowApp:
    """Initialized apflow application with all components wired."""

    def __init__(
        self,
        session: "Union[Session, AsyncSession]",
        task_manager: "TaskManager",
        task_creator: "TaskCreator",
        task_repository: "TaskRepository",
        registry: "Registry",
    ) -> None:
        self.session = session
        self.task_manager = task_manager
        self.task_creator = task_creator
        self.task_repository = task_repository
        self.registry = registry


def create_app(
    connection_string: Optional[str] = None,
    namespace: str = "apflow",
    cluster: bool = False,
) -> ApflowApp:
    """Create and initialize the full apflow application stack.

    Bootstraps: session → TaskManager → TaskCreator → TaskRepository → Registry.

    Args:
        connection_string: Database connection string. If None, uses defaults
            (APFLOW_DATABASE_URL env var, or SQLite file).
        namespace: apcore module namespace (default: "apflow").

    Returns:
        ApflowApp with all components ready to use.

    Example:
        app = create_app()
        # Start A2A server
        from apcore_a2a import serve
        serve(app.registry, name="apflow")
    """
    from apflow.core.storage.factory import create_session
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
    from apflow.core.execution.task_creator import TaskCreator
    from apflow.core.execution.task_manager import TaskManager
    from apflow.bridge import create_apflow_registry
    from apflow.durability import CheckpointManager, CircuitBreakerRegistry, RetryManager
    from apflow.governance import BudgetManager, PolicyEngine

    # Resolve connection string
    if connection_string is None:
        cm = get_config_manager()
        connection_string = cm.get("storage.connection_string")

    # Create session
    session = create_session(connection_string=connection_string)
    logger.info("Database session created")

    # Create core components
    task_repository = TaskRepository(session)
    task_creator = TaskCreator(session)

    # Create durability components
    checkpoint_manager = CheckpointManager(session)
    retry_manager = RetryManager(checkpoint_manager=checkpoint_manager)
    circuit_breaker_registry = CircuitBreakerRegistry()

    # Create governance components
    budget_manager = BudgetManager(task_repository)
    policy_engine = PolicyEngine()

    # Create task manager with durability + governance
    task_manager = TaskManager(
        session,
        checkpoint_manager=checkpoint_manager,
        retry_manager=retry_manager,
        circuit_breaker_registry=circuit_breaker_registry,
        budget_manager=budget_manager,
        policy_engine=policy_engine,
    )
    logger.info("TaskManager initialized with durability + governance")

    # Create apcore registry
    registry = create_apflow_registry(
        task_manager=task_manager,
        task_creator=task_creator,
        task_repository=task_repository,
        namespace=namespace,
    )
    logger.info(f"apcore Registry populated ({len(list(registry.list()))} modules)")

    # Start distributed runtime if cluster mode
    if cluster:
        try:
            from apflow.core.distributed.config import DistributedConfig
            from apflow.core.distributed.runtime import DistributedRuntime

            dist_config = DistributedConfig.from_env()
            DistributedRuntime(dist_config)  # Starts background tasks
            logger.info(f"Distributed runtime enabled (node: {dist_config.node_id})")
        except Exception as e:
            logger.warning(f"Distributed runtime failed to initialize: {e}")

    return ApflowApp(
        session=session,
        task_manager=task_manager,
        task_creator=task_creator,
        task_repository=task_repository,
        registry=registry,
    )
