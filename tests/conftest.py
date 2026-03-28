"""
Test configuration and fixtures for apflow
"""

import os
import uuid

import pytest
import pytest_asyncio
import sys
import signal
from unittest.mock import Mock, AsyncMock, patch
from typing import Optional

# Add project root to Python path for development
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Set DATABASE_URL to a unique SQLite file for the whole test session before any apflow or DB-related imports
test_db_file = f"{project_root}/.data/apflow_{uuid.uuid4().hex[:8]}.test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{test_db_file}"
print(f"set DATABASE_URL: {os.getenv('DATABASE_URL')}")

# IMPORTANT: Set other environment variables BEFORE any imports that might use them
# This prevents CrewAI, LiteLLM, and OpenTelemetry from starting background threads
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("CREWAI_DISABLE_EVENT_BUS", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", "all")
os.environ.setdefault("LITELLM_LOG", "ERROR")
os.environ.setdefault("LITELLM_TURN_OFF_MESSAGE_LOGGING", "true")
os.environ.setdefault("LITELLM_TURN_OFF_LOGGING", "true")


# Add src directory to path for imports
src_path = os.path.join(project_root, "src")
if src_path not in sys.path:
    sys.path.insert(0, src_path)

# Load environment variables from .env file
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed

# Auto-discover built-in extensions for tests
# This ensures extensions are registered before tests run
# Must be imported before other modules that use the registry
# NOTE: CrewAI and LLM extensions are NOT imported here to prevent background threads
# They will be imported lazily when needed by specific tests
try:
    from apflow.extensions.stdio import SystemInfoExecutor, CommandExecutor  # noqa: F401
except ImportError:
    pass  # Extension not available, tests will handle this

try:
    from apflow.extensions.http import RestExecutor  # noqa: F401
except ImportError:
    pass  # Extension not available, tests will handle this

# DO NOT import CrewAI here - it starts EventBus background thread immediately
# CrewAI extensions will be imported lazily when needed by tests
# try:
#     from apflow.extensions.crewai import CrewaiExecutor, BatchCrewaiExecutor  # noqa: F401
# except ImportError:
#     pass  # Extension not available, tests will handle this

try:
    from apflow.extensions.core import AggregateResultsExecutor  # noqa: F401
except ImportError:
    pass  # Extension not available, tests will handle this

try:
    from apflow.extensions.generate import GenerateExecutor  # noqa: F401
except ImportError:
    pass  # Extension not available, tests will handle this

# DO NOT import LLM here - it starts OpenTelemetry background threads immediately
# LLM extensions will be imported lazily when needed by tests
# try:
#     from apflow.extensions.llm import LLMExecutor  # noqa: F401
# except ImportError:
#     pass  # Extension not available, tests will handle this

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
)
from apflow.core.storage.sqlalchemy.models import Base, TASK_TABLE_NAME  # noqa: E402
from apflow.core.storage.factory import (  # noqa: E402
    create_session,
    get_default_session,
    reset_default_session,
    set_default_session,
    is_postgresql_url,
    normalize_postgresql_url,
)
from apflow.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

# Backward compatibility aliases
create_storage = create_session
get_default_storage = get_default_session


# Custom pytest markers
def pytest_configure(config):
    """Configure custom pytest markers"""
    config.addinivalue_line("markers", "requires_api_keys: mark test as requiring API keys")
    config.addinivalue_line(
        "markers", "integration: mark test as integration test that requires external services"
    )
    config.addinivalue_line("markers", "slow: mark test as slow")
    config.addinivalue_line("markers", "manual: mark test file as manual only (skip by default)")

    # Note: Environment variables are set at module level (before imports)
    # to prevent CrewAI, LiteLLM, and OpenTelemetry from starting background threads


def _get_test_database_url() -> Optional[str]:
    """Get test database URL from environment variable"""
    return os.getenv("APFLOW_TEST_DATABASE_URL") or os.getenv("TEST_DATABASE_URL")


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_db_file():
    yield
    db_file = os.environ["DATABASE_URL"].replace("sqlite:///", "")
    print("cleanup_test_db_file called")
    print("db_file:", db_file, "exists:", os.path.exists(db_file))
    if db_file.endswith(".test.db") and os.path.exists(db_file):
        try:
            print("Removing test database file:", db_file)
            os.remove(db_file)
        except Exception:
            pass  # Ignore errors


@pytest.fixture(scope="function")
def temp_db_path(tmp_path):
    """Create a temporary database file path (only used for DuckDB)"""
    test_db_url = _get_test_database_url()

    # If using PostgreSQL, don't create temp file
    if test_db_url and is_postgresql_url(test_db_url):
        logger.info("Using PostgreSQL database for testing")
        yield None
        return

    # Use temporary file-based DuckDB instead of in-memory
    # This ensures all code paths accessing the database see the same data
    # In-memory databases are isolated per connection, causing table creation issues
    db_file = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
    logger.info(f"Using temporary DuckDB file for testing: {db_file}")
    yield str(db_file)


@pytest.fixture(scope="function")
def sync_db_session(temp_db_path):
    """
    Create a synchronous database session for testing

    Supports both DuckDB (default) and PostgreSQL (via TEST_DATABASE_URL).
    Each test gets a fresh database session with automatic cleanup:
    - Tables are created fresh for each test
    - Data is cleaned up after each test to ensure test isolation
    """
    test_db_url = _get_test_database_url()

    # Use PostgreSQL if TEST_DATABASE_URL is set and is PostgreSQL
    if test_db_url and is_postgresql_url(test_db_url):
        logger.info(f"Using PostgreSQL database for testing: {test_db_url}")
        # Normalize connection string for sync mode
        connection_string = normalize_postgresql_url(test_db_url, async_mode=False)

        # Create engine with PostgreSQL
        engine = create_engine(connection_string, echo=False)

        # For PostgreSQL, we need to drop and recreate tables to ensure schema matches
        # PostgreSQL is a shared database, so previous tests may have created tables
        # with custom fields (e.g., priority_level from custom TaskModel tests)
        # Note: This is necessary for PostgreSQL but not for DuckDB (which uses new files each time)
        try:
            Base.metadata.drop_all(engine)
        except Exception as e:
            logger.warning(f"Error dropping tables (may not exist): {e}")

        # Create tables fresh for each test
        Base.metadata.create_all(engine)

        # Create session
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()

        try:
            # Ensure session is clean at the start of each test
            session.expire_all()
            yield session
        finally:
            # Cleanup: Expire all objects to clear session cache
            try:
                session.expire_all()
            except Exception:
                pass

            # Cleanup: Rollback any pending transactions first
            try:
                session.rollback()
            except Exception:
                pass

            # Cleanup: Delete all data from tables to ensure test isolation
            # Use a new transaction to avoid InFailedSqlTransaction errors
            try:
                # Start a new transaction for cleanup
                session.begin()
                session.execute(text(f"DELETE FROM {TASK_TABLE_NAME}"))
                session.commit()
            except Exception as e:
                # If cleanup fails, rollback and try to continue
                try:
                    session.rollback()
                except Exception:
                    pass
                logger.debug(f"Cleanup failed (non-critical): {e}")

            # Cleanup: Expire all objects again after cleanup
            try:
                session.expire_all()
            except Exception:
                pass

            # Close session and dispose engine
            try:
                session.close()
            except Exception:
                pass
            try:
                engine.dispose()
            except Exception:
                pass
    else:
        # Use DuckDB (default behavior)
        logger.info(f"Using DuckDB database for testing: {temp_db_path}")
        # Ensure file doesn't exist (cleanup from previous failed test)
        if temp_db_path and os.path.exists(temp_db_path):
            try:
                os.unlink(temp_db_path)
            except Exception:
                pass

        # Create engine with DuckDB
        engine = create_engine(f"sqlite:///{temp_db_path}", echo=False)

        # For DuckDB, we don't need to drop tables because each test uses a new file
        # The file is deleted after each test, so schema is always fresh
        Base.metadata.create_all(engine)

        # Create session
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()

        try:
            # Ensure session is clean at the start of each test
            session.expire_all()
            yield session
        finally:
            # Cleanup: Expire all objects to clear session cache
            try:
                session.expire_all()
            except Exception:
                pass

            # Cleanup: Rollback any pending transactions
            try:
                session.rollback()
            except Exception:
                pass

            # Cleanup: Delete all data from tables to ensure test isolation
            try:
                # Delete all tasks to ensure test isolation
                # Note: Using TASK_TABLE_NAME constant for table name
                session.execute(text(f"DELETE FROM {TASK_TABLE_NAME}"))
                session.commit()
            except Exception:
                try:
                    session.rollback()
                except Exception:
                    pass

            # Cleanup: Expire all objects again after cleanup
            try:
                session.expire_all()
            except Exception:
                pass

            # Close session and dispose engine
            try:
                session.close()
            except Exception:
                pass

            try:
                engine.dispose()
            except Exception:
                pass

            # Remove database file (only after session is closed)
            try:
                if temp_db_path and os.path.exists(temp_db_path):
                    os.unlink(temp_db_path)
            except Exception:
                pass  # Ignore cleanup errors


@pytest_asyncio.fixture(scope="function")
async def async_db_session(temp_db_path):
    """
    Create an async database session for testing

    Supports both DuckDB (mock) and PostgreSQL (real async session via TEST_DATABASE_URL).

    Note: DuckDB doesn't support async drivers, so SQLAlchemy won't allow
    creating AsyncEngine with DuckDB. When using DuckDB, this fixture creates a mock AsyncSession
    that properly implements isinstance checks for testing TaskManager's is_async logic.

    When TEST_DATABASE_URL is set to PostgreSQL, this fixture creates a real AsyncSession
    with asyncpg driver, providing true async operations.
    """
    test_db_url = _get_test_database_url()

    # Use PostgreSQL if TEST_DATABASE_URL is set and is PostgreSQL
    if test_db_url and is_postgresql_url(test_db_url):
        logger.info(f"Using PostgreSQL database for async testing: {test_db_url}")
        # Normalize connection string for async mode
        connection_string = normalize_postgresql_url(test_db_url, async_mode=True)

        # Create async engine with PostgreSQL
        engine = create_async_engine(connection_string, echo=False)

        # For PostgreSQL async, we need to drop and recreate tables to ensure schema matches
        # PostgreSQL is a shared database, so previous tests may have created tables
        # with custom fields (e.g., priority_level from custom TaskModel tests)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
        except Exception as e:
            logger.warning(f"Error dropping tables (may not exist): {e}")

        # Create tables fresh for each test
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # Create async session
        SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        session = SessionLocal()

        try:
            yield session
        finally:
            # Cleanup: Rollback any pending transactions first
            try:
                await session.rollback()
            except Exception:
                pass

            # Cleanup: Delete all data from tables to ensure test isolation
            # Use a new transaction to avoid InFailedSqlTransaction errors
            try:
                # Start a new transaction for cleanup
                await session.begin()
                await session.execute(text(f"DELETE FROM {TASK_TABLE_NAME}"))
                await session.commit()
            except Exception as e:
                # If cleanup fails, rollback and try to continue
                try:
                    await session.rollback()
                except Exception:
                    pass
                logger.debug(f"Cleanup failed (non-critical): {e}")

            # Close session and dispose engine
            try:
                await session.close()
            except Exception:
                pass
            try:
                await engine.dispose()
            except Exception:
                pass
    else:
        # Use mock AsyncSession for DuckDB (DuckDB doesn't support async)
        logger.info("Using mock AsyncSession for DuckDB (DuckDB doesn't support async drivers)")
        from unittest.mock import AsyncMock, MagicMock

        # Note: AsyncSession is already imported at module level, don't re-import here
        # Create a mock that will pass isinstance checks
        # We subclass MagicMock and set __class__ to make isinstance work
        class MockAsyncSession(AsyncSession):
            """Mock AsyncSession that passes isinstance checks"""

            def __init__(self):
                # Don't call super().__init__() to avoid requiring real engine
                pass

        # Create instance and configure mock methods
        mock_session = MockAsyncSession()
        mock_session.commit = AsyncMock()
        mock_session.close = AsyncMock()
        mock_session.refresh = AsyncMock()
        mock_session.add = MagicMock()
        mock_session.get = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.query = MagicMock()
        mock_session.bind = None

        yield mock_session

        # Cleanup
        try:
            await mock_session.close()
        except Exception:
            pass


@pytest.fixture(scope="function")
def mock_storage():
    """Create a mock storage instance"""
    storage = Mock()
    storage.save_task = AsyncMock(return_value=True)
    storage.get_task = AsyncMock(return_value=None)
    storage.update_task = AsyncMock(return_value=True)
    storage.list_tasks = AsyncMock(return_value=[])
    storage.delete_task = AsyncMock(return_value=True)
    storage.close = AsyncMock()
    return storage


@pytest.fixture(scope="function")
def sample_task_data():
    """Sample task data for testing"""
    return {
        "id": "test-task-1",
        "parent_id": None,
        "user_id": "test-user-123",
        "name": "Test Task",
        "status": "pending",
        "priority": 1,
        "has_children": False,
        "dependencies": [],
        "progress": 0.0,
        "inputs": {"url": "https://example.com"},
        "params": {},
        "schemas": {
            "method": "crewai_executor",
            "input_schema": {"properties": {"url": {"type": "string", "required": True}}},
        },
        "result": None,
        "error": None,
    }


@pytest.fixture(scope="function")
def sample_task_tree_data():
    """Sample task tree data for testing"""
    return {
        "tasks": [
            {
                "id": "root-task",
                "parent_id": None,
                "user_id": "test-user-123",
                "name": "Root Task",
                "status": "pending",
                "priority": 3,
                "has_children": True,
                "dependencies": [
                    {"id": "child-1", "required": True},
                    {"id": "child-2", "required": True},
                ],
                "schemas": {"method": "aggregate_results_executor"},
            },
            {
                "id": "child-1",
                "parent_id": "root-task",
                "user_id": "test-user-123",
                "name": "Child Task 1",
                "status": "pending",
                "priority": 1,
                "has_children": False,
                "dependencies": [],
                "schemas": {
                    "method": "crewai_executor",
                    "input_schema": {"properties": {"url": {"type": "string", "required": True}}},
                },
                "inputs": {"url": "https://example.com"},
            },
            {
                "id": "child-2",
                "parent_id": "root-task",
                "user_id": "test-user-123",
                "name": "Child Task 2",
                "status": "pending",
                "priority": 1,
                "has_children": False,
                "dependencies": [{"id": "child-1", "required": True}],
                "schemas": {
                    "method": "crewai_executor",
                    "input_schema": {"properties": {"url": {"type": "string", "required": True}}},
                },
                "inputs": {"url": "https://example.com"},
            },
        ]
    }


@pytest_asyncio.fixture(scope="function")
async def task_manager_with_session(async_db_session):
    """
    Fixture providing a TaskManager instance with an async database session.

    Returns a tuple of (TaskManager, AsyncSession) for tests that need to
    execute tasks and check their status in the database.

    Usage:
        async def test_something(self, task_manager_with_session):
            task_manager, session = task_manager_with_session
            # Use both task_manager and session
    """
    from apflow.core.execution.task_manager import TaskManager

    # Create TaskManager with async session
    task_manager = TaskManager(db=async_db_session, executor_instances={})

    return (task_manager, async_db_session)


@pytest.fixture(autouse=True)
def reset_storage_singleton():
    """Reset storage singleton before each test"""
    reset_default_session()
    yield
    reset_default_session()


@pytest.fixture(autouse=True)
def ensure_clean_db_session(request):
    """
    Ensure database session is clean at the start of each test.

    This fixture runs automatically and checks if the test uses sync_db_session.
    If it does, it ensures that the session cache is cleared and any pending
    transactions are rolled back before the test starts.
    """
    # Check if test uses sync_db_session fixture
    if "sync_db_session" in request.fixturenames:
        sync_db_session = request.getfixturevalue("sync_db_session")
        # Expire all objects in the session to clear cache
        try:
            sync_db_session.expire_all()
        except Exception:
            pass

        # Rollback any pending transactions
        try:
            sync_db_session.rollback()
        except Exception:
            pass

    yield

    # Cleanup after test
    if "sync_db_session" in request.fixturenames:
        sync_db_session = request.getfixturevalue("sync_db_session")
        try:
            sync_db_session.expire_all()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def ensure_executors_registered():
    """
    Ensure all required executors are registered before each test

    This fixture ensures that executors are registered even if previous tests
    cleared the ExtensionRegistry. This is necessary because ExtensionRegistry
    is a singleton and some tests (like test_main.py) clear it in setup_method.

    The fixture explicitly re-registers executors using override=True to ensure
    they are available even if registry was cleared by previous tests.
    """
    from apflow.core.extensions import get_registry
    from apflow.core.extensions.types import ExtensionCategory

    registry = get_registry()

    # Helper function to register executor if not already registered
    def ensure_registered(executor_class, executor_id: str):
        """Register executor if not already registered"""
        if not registry.is_registered(executor_id):
            try:
                # Try to create a template instance
                try:
                    template = executor_class(inputs={})
                except Exception:
                    # If instantiation fails, create minimal template
                    class TemplateClass(executor_class):
                        def __init__(self):
                            pass

                    template = TemplateClass()
                    template.id = getattr(executor_class, "id", executor_id)
                    template.name = getattr(executor_class, "name", executor_class.__name__)
                    template.description = getattr(executor_class, "description", "")
                    template.category = ExtensionCategory.EXECUTOR
                    template.type = getattr(executor_class, "type", "default")

                # Register with override=True to force re-registration
                registry.register(extension=template, executor_class=executor_class, override=True)
            except Exception:
                # Ignore registration errors - some executors may not be available
                pass

    # Ensure all required executors are registered
    try:
        from apflow.extensions.stdio import SystemInfoExecutor, CommandExecutor

        ensure_registered(SystemInfoExecutor, "system_info_executor")
        ensure_registered(CommandExecutor, "command_executor")
    except ImportError:
        pass

    # DO NOT import CrewAI here - it starts EventBus background thread immediately
    # CrewAI will be imported lazily when needed by specific tests
    # This prevents background threads from running in all tests
    # try:
    #     from apflow.extensions.crewai import CrewaiExecutor, BatchCrewaiExecutor
    #     ensure_registered(CrewaiExecutor, "crewai_executor")
    #     ensure_registered(BatchCrewaiExecutor, "batch_crewai_executor")
    # except ImportError:
    #     pass

    try:
        from apflow.extensions.core import AggregateResultsExecutor

        ensure_registered(AggregateResultsExecutor, "aggregate_results_executor")
    except ImportError:
        pass

    try:
        from apflow.extensions.generate import GenerateExecutor

        ensure_registered(GenerateExecutor, "generate_executor")
    except ImportError:
        pass

    # DO NOT import LLM here - it starts OpenTelemetry background threads immediately
    # LLM will be imported lazily when needed by specific tests
    # This prevents background threads from running in all tests
    # try:
    #     from apflow.extensions.llm import LLMExecutor
    #     ensure_registered(LLMExecutor, "llm_executor")
    # except ImportError:
    #     pass

    yield

    # No cleanup needed - registry state persists between tests
    # (which is the desired behavior for most tests)


@pytest.fixture(scope="function")
def use_test_db_session(sync_db_session, temp_db_path):
    """
    Fixture to set and reset default session for tests - uses test database

    This fixture ensures that all tests use the test database
    instead of the persistent database file, preventing data pollution.

    For DuckDB: Uses a temporary file-based database (not in-memory) to ensure
    all code paths see the same database with the same tables.

    For PostgreSQL: Uses the TEST_DATABASE_URL environment variable.

    Usage:
        - Add `use_test_db_session` parameter to your test function
        - The fixture will automatically set the default session to the test database
        - After the test, it will reset the default session

    Example:
        async def test_something(self, use_test_db_session):
            # get_default_session() will return the test database session
            session = get_default_session()
            ...
    """
    set_default_session(sync_db_session)

    # Set database URL environment variable to ensure all code paths use the test database
    # This is crucial for DuckDB file-based tests where multiple engine creations must point to the same file
    old_test_db_url = os.environ.get("TEST_DATABASE_URL")
    old_apflow_db_url = os.environ.get("APFLOW_DATABASE_URL")
    old_db_url = os.environ.get("DATABASE_URL")

    test_db_url = _get_test_database_url()
    if test_db_url and is_postgresql_url(test_db_url):
        # PostgreSQL - use TEST_DATABASE_URL
        os.environ["DATABASE_URL"] = test_db_url
    elif temp_db_path:
        # DuckDB file - set DATABASE_URL to point to the temp file
        duckdb_url = f"sqlite:///{temp_db_path}"
        os.environ["DATABASE_URL"] = duckdb_url
        logger.debug(f"Set test DATABASE_URL to: {duckdb_url}")

    # Patch create_pooled_session to return the test session
    # This ensures that code using create_pooled_session() gets the test session
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_create_pooled_session_impl():
        yield sync_db_session

    def mock_create_pooled_session(*args, **kwargs):
        return mock_create_pooled_session_impl()

    # Patch the factory function, route/executor imports, and the public re-export
    # so that lazy imports (webhook.py, internal.py, ical.py) also resolve to the test session
    with (
        patch(
            "apflow.core.storage.factory.create_pooled_session",
            side_effect=mock_create_pooled_session,
        ),
        patch(
            "apflow.core.execution.task_executor.create_pooled_session",
            side_effect=mock_create_pooled_session,
        ),
        patch(
            "apflow.core.storage.create_pooled_session",
            side_effect=mock_create_pooled_session,
        ),
    ):
        yield sync_db_session

    # Restore environment variables
    if old_db_url is not None:
        os.environ["DATABASE_URL"] = old_db_url
    elif "DATABASE_URL" in os.environ:
        del os.environ["DATABASE_URL"]

    if old_apflow_db_url is not None:
        os.environ["APFLOW_DATABASE_URL"] = old_apflow_db_url
    elif "APFLOW_DATABASE_URL" in os.environ:
        del os.environ["APFLOW_DATABASE_URL"]

    if old_test_db_url is not None:
        os.environ["TEST_DATABASE_URL"] = old_test_db_url
    elif "TEST_DATABASE_URL" in os.environ:
        del os.environ["TEST_DATABASE_URL"]

    reset_default_session()


@pytest.fixture(scope="function")
def fresh_db_session(temp_db_path):
    """
    Create a fresh database session with tables dropped and recreated

    This fixture is specifically for tests that need custom TaskModel with additional fields.
    It drops and recreates tables to ensure schema matches the current TaskModel.

    Performance note: This fixture has higher overhead than sync_db_session because it
    drops and recreates tables. Only use this fixture when you need to test custom TaskModel
    or when schema changes are required.

    Usage:
        - Use this fixture instead of sync_db_session when testing custom TaskModel
        - Example: def test_custom_model(self, fresh_db_session):
    """
    test_db_url = _get_test_database_url()

    # Use PostgreSQL if TEST_DATABASE_URL is set and is PostgreSQL
    if test_db_url and is_postgresql_url(test_db_url):
        logger.info(f"Using PostgreSQL database with fresh tables: {test_db_url}")
        connection_string = normalize_postgresql_url(test_db_url, async_mode=False)
        engine = create_engine(connection_string, echo=False)

        # Drop and recreate tables to ensure schema matches
        try:
            Base.metadata.drop_all(engine)
        except Exception as e:
            logger.warning(f"Error dropping tables (may not exist): {e}")

        Base.metadata.create_all(engine)

        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()

        try:
            yield session
        finally:
            try:
                session.rollback()
            except Exception:
                pass

            try:
                session.begin()
                session.execute(text(f"DELETE FROM {TASK_TABLE_NAME}"))
                session.commit()
            except Exception as e:
                try:
                    session.rollback()
                except Exception:
                    pass
                logger.debug(f"Cleanup failed (non-critical): {e}")

            try:
                session.close()
            except Exception:
                pass
            try:
                engine.dispose()
            except Exception:
                pass
    else:
        # Use DuckDB (default behavior)
        logger.info(f"Using DuckDB database with fresh tables: {temp_db_path}")
        if temp_db_path and os.path.exists(temp_db_path):
            try:
                os.unlink(temp_db_path)
            except Exception:
                pass

        engine = create_engine(f"sqlite:///{temp_db_path}", echo=False)

        # Drop and recreate tables to ensure schema matches
        try:
            Base.metadata.drop_all(engine)
        except Exception as e:
            logger.warning(f"Error dropping tables (may not exist): {e}")

        Base.metadata.create_all(engine)

        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()

        try:
            yield session
        finally:
            try:
                session.rollback()
            except Exception:
                pass

            try:
                session.execute(text(f"DELETE FROM {TASK_TABLE_NAME}"))
                session.commit()
            except Exception:
                session.rollback()

            session.close()
            engine.dispose()

            try:
                if temp_db_path and os.path.exists(temp_db_path):
                    os.unlink(temp_db_path)
            except Exception:
                pass


@pytest.fixture(autouse=True)
def mock_logger():
    """Mock logger for testing"""
    with patch("apflow.logger.get_logger") as mock_logger:
        logger = Mock()
        logger.info = Mock()
        logger.debug = Mock()
        logger.warning = Mock()
        logger.error = Mock()
        mock_logger.return_value = logger
        yield logger


@pytest.fixture
def api_keys_available():
    """Check if required API keys are available for integration tests"""
    openai_key = os.getenv("OPENAI_API_KEY")

    if not openai_key:
        pytest.skip("OPENAI_API_KEY is not set - skipping integration test")

    return {"openai_api_key": openai_key}


def requires_api_keys(func):
    """Decorator to mark tests that require API keys"""
    return pytest.mark.requires_api_keys(pytest.mark.asyncio(func))


def integration_test(func):
    """Decorator to mark integration tests that require external services"""
    return pytest.mark.integration(pytest.mark.requires_api_keys(pytest.mark.asyncio(func)))


@pytest.fixture(autouse=True)
def enable_faulthandler():
    """
    Enable faulthandler to print stack traces on hang.

    This helps identify where tests hang by printing stack traces
    when the process receives SIGUSR1 (kill -USR1 <pid>).
    """
    import faulthandler
    import sys

    # Enable faulthandler to dump stack traces on hang
    # You can trigger it manually with: kill -USR1 <pid>
    faulthandler.enable()

    # Also dump on SIGUSR2 for Python 3.8+
    if hasattr(faulthandler, "register"):
        try:
            faulthandler.register(signal.SIGUSR2, file=sys.stderr, all_threads=True)
        except (AttributeError, OSError):
            pass

    yield

    # Keep faulthandler enabled for debugging


@pytest.fixture(autouse=True)
def cleanup_test_environment():
    """
    Comprehensive cleanup fixture to ensure each test starts with a clean environment.

    This fixture resets all global state, singletons, and registries between tests
    to prevent state pollution and ensure test isolation.

    Cleans up:
    - Event loops and async resources
    - CLI registry
    - Config registry
    - Extension registry
    - Executor registry
    - Tool registry
    - TaskExecutor singleton
    - Session registry state
    - Thread-local state
    - Asyncio tasks and EventQueueBridge instances
    """
    import asyncio
    import gc
    import logging

    # Cleanup before test (in case previous test left state)
    _cleanup_all_global_state()

    yield

    # Cleanup after test
    # First, stop any background threads from CrewAI, LiteLLM, or OpenTelemetry
    _stop_background_threads()

    # Then, cancel all pending asyncio tasks to prevent them from interfering with cleanup
    # This is critical to prevent EventQueueBridge worker tasks from causing deadlocks
    try:
        loop = asyncio.get_event_loop()
        if not loop.is_closed():
            try:
                # Get all pending tasks
                tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]

                # Cancel all tasks
                for task in tasks:
                    try:
                        task.cancel()
                    except Exception:
                        pass

                # Wait for tasks to finish cancelling (with timeout)
                if tasks and not loop.is_running():
                    try:
                        # Use asyncio.wait_for with a short timeout
                        loop.run_until_complete(
                            asyncio.wait_for(
                                asyncio.gather(*tasks, return_exceptions=True), timeout=0.5
                            )
                        )
                    except (asyncio.TimeoutError, Exception):
                        # If timeout or error, just continue - tasks are cancelled
                        pass
            except Exception:
                # If anything fails, just continue
                pass
    except RuntimeError:
        # No event loop, that's fine
        pass

    # Temporarily disable logging to prevent deadlock during cleanup
    # This prevents background threads from trying to log during gc.collect()
    logging.disable(logging.CRITICAL)

    try:
        # Cleanup global state
        _cleanup_all_global_state()

        # Force garbage collection to clean up any lingering resources
        # This is done with logging disabled to prevent deadlocks
        gc.collect()
    finally:
        # Re-enable logging after cleanup
        logging.disable(logging.NOTSET)

    # Reset event loop in current thread to prevent state pollution
    # This is safe because pytest-asyncio manages event loops per test
    try:
        asyncio.set_event_loop(None)
    except Exception:
        pass


def _stop_background_threads():
    """
    Stop background threads from CrewAI, LiteLLM, and OpenTelemetry.

    These libraries start background threads on import, which can cause
    tests to hang during cleanup. This function attempts to stop them.
    """
    import threading
    import time

    # Try to stop CrewAI EventBus if it exists
    try:
        from crewai.events.event_bus import EventBus

        # EventBus is a singleton, try to stop it
        if hasattr(EventBus, "_instance") and EventBus._instance:
            try:
                event_bus = EventBus._instance

                # Try to stop using the stop method if available
                if hasattr(event_bus, "stop"):
                    try:
                        event_bus.stop()
                    except Exception:
                        pass

                # Try to stop the event loop directly
                if hasattr(event_bus, "_loop") and event_bus._loop:
                    try:
                        loop = event_bus._loop
                        if not loop.is_closed():
                            loop.call_soon_threadsafe(loop.stop)
                    except Exception:
                        pass

                # Forcefully stop the background thread if it exists
                # Find the CrewAIEventsLoop thread and stop it
                for thread in threading.enumerate():
                    if "CrewAIEventsLoop" in thread.name or (
                        hasattr(thread, "_target")
                        and hasattr(thread._target, "__self__")
                        and hasattr(thread._target.__self__, "_loop")
                    ):
                        # Try to stop the loop from the thread
                        try:
                            if hasattr(event_bus, "_loop") and event_bus._loop:
                                event_bus._loop.call_soon_threadsafe(event_bus._loop.stop)
                        except Exception:
                            pass

                        # Give it a moment to stop
                        time.sleep(0.1)

                        # If still alive, it's a daemon thread so it will die with the process
                        # But we can't forcefully kill it without risking corruption
                        break

            except Exception:
                pass
    except (ImportError, AttributeError):
        pass

    # Try to stop OpenTelemetry processors
    try:
        # Force flush and shutdown any processors
        import opentelemetry.sdk.trace as trace_sdk

        if hasattr(trace_sdk, "_global_tracer_provider"):
            provider = trace_sdk._global_tracer_provider
            if provider and hasattr(provider, "shutdown"):
                try:
                    provider.shutdown()
                except Exception:
                    pass
    except (ImportError, AttributeError):
        pass

    # Try to stop LiteLLM callbacks/threads
    try:
        import litellm

        # LiteLLM doesn't have explicit shutdown, but we can clear callbacks
        if hasattr(litellm, "callback_list"):
            try:
                litellm.callback_list = []
            except Exception:
                pass
        if hasattr(litellm, "success_callback"):
            try:
                litellm.success_callback = []
            except Exception:
                pass
        if hasattr(litellm, "failure_callback"):
            try:
                litellm.failure_callback = []
            except Exception:
                pass
    except (ImportError, AttributeError):
        pass


def _cleanup_all_global_state():
    """
    Clean up all global state, singletons, and registries.

    This function is called before and after each test to ensure
    complete isolation between tests.
    """
    # 1. Clean up CLI registry
    try:
        from apflow.cli.decorators import _cli_registry

        _cli_registry.clear()
    except (ImportError, AttributeError):
        pass

    # 2. Clean up Config registry
    try:
        from apflow.core.config.registry import get_config_manager

        cm = get_config_manager()
        cm.clear()
    except (ImportError, AttributeError):
        pass

    # 3. Clean up Extension registry (both singleton and global instance)
    try:
        from apflow.core.extensions.registry import ExtensionRegistry, _registry

        # Reset singleton
        ExtensionRegistry._instance = None
        # Clear global instance state
        if hasattr(_registry, "_by_id"):
            _registry._by_id.clear()
        if hasattr(_registry, "_by_category"):
            _registry._by_category.clear()
        if hasattr(_registry, "_factory_functions"):
            _registry._factory_functions.clear()
        if hasattr(_registry, "_executor_classes"):
            _registry._executor_classes.clear()
        # Reset extensions loaded flag (imported as module-level, need to handle carefully)
        import apflow.core.extensions.registry as registry_module

        registry_module._extensions_loaded = False
    except (ImportError, AttributeError):
        pass

    # 3a. Clean up extension manager's loaded extensions tracking
    try:
        from apflow.core.extensions.manager import _loaded_extensions

        _loaded_extensions.clear()
    except (ImportError, AttributeError):
        pass

    # 4. Clean up Executor registry (both singleton and global instance)
    try:
        from apflow.core.execution.executor_registry import ExecutorRegistry, _registry

        # Reset singleton
        ExecutorRegistry._instance = None
        # Clear global instance state
        if hasattr(_registry, "_executors"):
            _registry._executors.clear()
        if hasattr(_registry, "_factory_functions"):
            _registry._factory_functions.clear()
    except (ImportError, AttributeError):
        pass

    # 5. Clean up Tool registry (both singleton and global instance)
    try:
        from apflow.core.tools.registry import ToolRegistry, _registry

        # Reset singleton
        ToolRegistry._instance = None
        # Clear global instance state
        if hasattr(_registry, "_tools"):
            _registry._tools.clear()
    except (ImportError, AttributeError):
        pass

    # 6. Clean up TaskExecutor singleton
    try:
        from apflow.core.execution.task_executor import TaskExecutor

        TaskExecutor._instance = None
        TaskExecutor._initialized = False
    except (ImportError, AttributeError):
        pass

    # 7. Clean up Session registry state
    try:
        from apflow.core.storage.factory import SessionRegistry

        SessionRegistry.reset_session_pool_manager()
        SessionRegistry.reset_default_session()
    except (ImportError, AttributeError):
        pass

    # 8. Clean up any thread-local state
    try:
        from apflow.core.config.registry import _thread_local

        # Clear thread-local storage
        for key in list(_thread_local.__dict__.keys()):
            try:
                delattr(_thread_local, key)
            except Exception:
                pass
    except (ImportError, AttributeError):
        pass

    # 9. Clean up TaskTracker state (if exists)
    try:
        from apflow.core.execution.task_tracker import TaskTracker

        # Reset singleton
        TaskTracker._instance = None
        TaskTracker._initialized = False
        # Clear running tasks if instance exists
        if TaskTracker._instance is not None and hasattr(TaskTracker._instance, "_running_tasks"):
            TaskTracker._instance._running_tasks.clear()
    except (ImportError, AttributeError):
        pass


@pytest.fixture(autouse=True)
def disable_executor_restrictions():
    """
    Fixture to disable executor restrictions for all tests.

    When APFLOW_EXTENSIONS is set in .env, it restricts executor access.
    This fixture clears it for all tests to allow test executors to run.

    This is automatically applied to all tests (autouse=True) to prevent
    permission errors when using custom test executors.

    Tests can still explicitly set APFLOW_EXTENSIONS using patch.dict
    to test permission checking behavior specifically.
    """
    from unittest.mock import patch
    import copy

    # Save the original environment
    original_env = copy.copy(os.environ)

    # Remove APFLOW_EXTENSIONS from environment for tests
    # This allows all registered executors to be used
    env_to_patch = {k: v for k, v in original_env.items() if k != "APFLOW_EXTENSIONS"}

    with patch.dict(os.environ, env_to_patch, clear=True):
        yield

    # Environment is automatically restored after the test


@pytest.fixture(scope="function")
def disable_api_for_tests():
    """
    Fixture to disable API usage and fix event loop issues in CLI tests.

    This fixture ensures that CLI commands use the local database instead of trying
    to connect to an API server during tests. It also patches run_async_safe to
    prevent event loop conflicts when tests are marked with @pytest.mark.asyncio.

    Usage:
        @pytest.mark.asyncio
        async def test_cli_command(self, use_test_db_session, disable_api_for_tests):
            # CLI will use local database, and run_async_safe won't conflict
            # with pytest-asyncio's event loop
    """
    from unittest.mock import patch
    import asyncio
    import logging

    # Disable all logging to prevent pollution of CLI output
    logging.disable(logging.CRITICAL)

    def patched_run_async_safe(coro):
        """
        Patched version of run_async_safe that works correctly in pytest-asyncio tests.

        Runs the coroutine in a separate thread to avoid event loop conflicts.
        """
        import concurrent.futures

        def run_in_thread():
            # Create a fresh event loop in this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(run_in_thread)
            return future.result()

    # Mock both should_use_api and run_async_safe
    with (
        patch("apflow.cli.api_gateway_helper.should_use_api", return_value=False),
        patch("apflow.cli.api_gateway_helper.run_async_safe", side_effect=patched_run_async_safe),
        patch("apflow.cli.commands.tasks.run_async_safe", side_effect=patched_run_async_safe),
    ):
        try:
            yield
        finally:
            # Re-enable logging after test
            logging.disable(logging.NOTSET)


@pytest.fixture(scope="function")
def run_async():
    """
    Helper fixture to run async code from synchronous tests.

    This is useful for CLI tests that need to set up async database operations
    without being marked as async tests themselves.

    Usage:
        def test_cli_command(self, use_test_db_session, run_async):
            task_id = run_async(task_repository.create_task(...))
            # Continue with sync code like runner.invoke()
    """
    import asyncio

    def _run_async(coro):
        """Run async code and return the result"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    return _run_async
