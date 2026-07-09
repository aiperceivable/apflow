"""
Schema migration manager

Discovers, tracks, and executes migrations in order.
Maintains migration history to ensure idempotent execution.

Migration discovery is automatic - all Migration subclasses in the migrations/
directory are discovered dynamically using Python reflection.
"""

import importlib
import inspect as inspect_module
from pathlib import Path
from typing import List, Set, Type
from sqlalchemy import Engine, inspect, text
from apflow.core.storage.sqlalchemy.models import TASK_TABLE_NAME
from apflow.core.storage.migrations import Migration
from apflow.logger import get_logger

logger = get_logger(__name__)


class MigrationHistoryTable:
    """Helper to create and manage migration history table"""

    TABLE_NAME = "apflow_schema_migrations"

    @staticmethod
    def ensure_exists(engine: Engine) -> None:
        """Create migration history table if not exists.

        Uses CREATE TABLE IF NOT EXISTS instead of a check-then-create so this
        is safe under concurrent multi-node startup: a separate existence check
        followed by a bare CREATE TABLE leaves a race window where another node
        can create the table first, causing this node's CREATE TABLE to crash.
        """
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE IF NOT EXISTS {MigrationHistoryTable.TABLE_NAME} (
                            id VARCHAR(100) PRIMARY KEY,
                            description TEXT NOT NULL,
                            apflow_version VARCHAR(50) NOT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                )
            logger.info(
                f"✓ Ensured migration history table exists: {MigrationHistoryTable.TABLE_NAME}"
            )
        except Exception as e:
            logger.error(f"✗ Failed to create migration history table: {str(e)}")
            raise

    @staticmethod
    def get_applied(engine: Engine) -> Set[str]:
        """Get set of applied migration IDs"""
        inspector = inspect(engine)

        if MigrationHistoryTable.TABLE_NAME not in inspector.get_table_names():
            return set()

        try:
            with engine.begin() as conn:
                result = conn.execute(text(f"SELECT id FROM {MigrationHistoryTable.TABLE_NAME}"))
                return {row[0] for row in result}
        except Exception as e:
            logger.warning(f"⚠ Could not read migration history: {str(e)}")
            return set()

    @staticmethod
    def record(engine: Engine, migration: Migration) -> None:
        """Record migration as applied with apflow version.

        Does not swallow write failures: run_pending() calls this right after
        migration.upgrade() succeeds, so a swallowed failure here would leave
        the migration silently unrecorded and re-attempted on every subsequent
        startup with no visible error.
        """
        from importlib.metadata import version

        __version__ = version("apflow")

        with engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {MigrationHistoryTable.TABLE_NAME} (id, description, apflow_version) "
                    f"VALUES (:id, :desc, :version)"
                ),
                {
                    "id": migration.id,
                    "desc": migration.description,
                    "version": __version__,
                },
            )
        logger.info(f"✓ Recorded migration '{migration.id}' (apflow v{__version__}) in history")


class MigrationManager:
    """Manages discovery and execution of migrations"""

    def __init__(self):
        self._migrations: List[Migration] = []
        self._discover_migrations()

    def _discover_migrations(self) -> None:
        """Dynamically discover all migration classes

        Scans all Python modules in the migrations/ directory for Migration subclasses.
        Sorts migrations by ID to ensure consistent execution order.
        """
        migrations_dir = Path(__file__).parent / "migrations"
        discovered: List[tuple[Type[Migration], str]] = []

        # Scan all Python files in migrations directory
        for module_path in sorted(migrations_dir.glob("*.py")):
            if module_path.name.startswith("_"):
                continue  # Skip __init__.py, _*.py

            module_name = f"apflow.core.storage.migrations.{module_path.stem}"

            try:
                module = importlib.import_module(module_name)

                # Find all Migration subclasses in the module
                for name, obj in inspect_module.getmembers(module):
                    if (
                        inspect_module.isclass(obj)
                        and issubclass(obj, Migration)
                        and obj is not Migration
                        and not name.startswith("_")
                    ):
                        discovered.append((obj, module_path.stem))
                        logger.debug(f"Discovered migration class: {obj.__name__}")
            except Exception as e:
                logger.warning(f"⚠ Could not load migration module {module_name}: {str(e)}")

        # Instantiate migrations, stamp each with its filename-derived id (the
        # documented contract — see each migration file's own docstring), and
        # sort by that id so execution order matches filename order (001, 002,
        # ...) rather than alphabetical class-name order.
        instances: List[Migration] = []
        for cls, filename_stem in discovered:
            instance = cls()
            instance.set_filename_id(filename_stem)
            instances.append(instance)

        self._migrations = sorted(instances, key=lambda m: m.id)

        logger.debug(
            f"Discovered {len(self._migrations)} migrations: {[m.id for m in self._migrations]}"
        )

    def run_pending(self, engine: Engine) -> None:
        """
        Run all pending migrations

        Args:
            engine: SQLAlchemy engine instance
        """
        # Ensure migration history table exists
        MigrationHistoryTable.ensure_exists(engine)

        # Check if task table exists (new installation doesn't need migration)
        inspector = inspect(engine)
        if TASK_TABLE_NAME not in inspector.get_table_names():
            logger.debug("New installation: no migrations needed")
            return

        # Get applied migrations
        applied = MigrationHistoryTable.get_applied(engine)

        # A migration is pending only if neither its id nor any of its aliases is
        # already recorded — so a migration recorded under an old alias id (after a
        # rename) is not re-run.
        pending = [
            m
            for m in self._migrations
            if m.id not in applied and not any(a in applied for a in getattr(m, "aliases", []))
        ]

        if not pending:
            logger.debug("✓ All migrations already applied")
            return

        logger.info(f"Running {len(pending)} pending migrations...")

        for migration in pending:
            try:
                logger.info(f"⏳ Running migration: {migration.id}")
                migration.upgrade(engine)
                MigrationHistoryTable.record(engine, migration)
                logger.info(f"✓ Completed migration: {migration.id}")
            except Exception as e:
                logger.error(f"✗ Migration failed: {migration.id}: {str(e)}")
                raise

        logger.info(f"✓ All {len(pending)} migrations completed successfully")

    def get_applied(self, engine: Engine) -> List[str]:
        """Get list of applied migration IDs"""
        return sorted(list(MigrationHistoryTable.get_applied(engine)))

    def get_pending(self, engine: Engine) -> List[str]:
        """Get list of pending migration IDs"""
        applied = MigrationHistoryTable.get_applied(engine)
        return [m.id for m in self._migrations if m.id not in applied]

    def get_all_migrations(self) -> List[Migration]:
        """Get all discovered migrations"""
        return self._migrations
