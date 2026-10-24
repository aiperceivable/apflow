---
description: Storage migration reference for SQLite, PostgreSQL, and the 0.24.0 service, Run, and usage schema upgrades.
---

# Feature Spec: Storage Migration (DuckDB to SQLite)

## Upgrading to 0.24.0 (SQLite and PostgreSQL)

The original DuckDB-to-SQLite specification follows this upgrade section.
The 0.24.0 upgrade adds schema migrations to the existing SQLite or PostgreSQL
database; it does not convert its dialect or reset historical tasks.

| Migration | Additions |
|---|---|
| `006_add_service_execution_contracts` | Task cancellation intent, structured errors, budget scope and execution identity; submission receipts, durable dispatch, token accounts/reservations, and invocation records |
| `007_add_workflow_runs_usage_events` | `WorkflowRun`, immutable `UsageEvent`, nullable task `run_id` / `source_task_id` / `result_snapshot_hash`, and invocation `run_id` |

`create_session()` and `create_app()` discover and apply pending migrations when
initializing their selected database. Applied IDs are stored in
`apflow_schema_migrations`; subsequent initialization skips recorded migrations.
Both SQLite and PostgreSQL are supported. There is no separate CLI migration
command. Initializing storage can perform DDL, so use the following upgrade order:

1. Stop dispatchers, workers, schedulers, and service processes that write to this
   database. Take a consistent database backup, including outstanding receipts and
   budget liabilities, and test the upgrade on a copy.
2. Install 0.24.0 and initialize the intended database once using the same URL as
   the service. Do not run old and new writers concurrently.
3. Inspect the migration history and representative old task statuses, results,
   and usage. Restart the upgraded services and check `/readyz` before admitting work.

```python
import os
from apflow.app import create_app
from apflow.core.storage.migrate import MigrationManager

app = create_app(connection_string=os.environ["APFLOW_DATABASE_URL"])
try:
    engine = app.session.get_bind().engine
    manager = MigrationManager()
    applied = manager.get_applied(engine)
    pending = manager.get_pending(engine)
    # Inspect applied and pending; IDs are full filename stems, without .py.
finally:
    app.session.close()
```

Migration 007 preserves recognizable nonempty legacy usage as baseline events;
inherited Copy/Link totals are not evidence of another consumption. These
baseline events remain available for historical reporting and are excluded from
`usage.events` billing delivery, so deployment does not charge old usage again.
Old task outcomes are preserved, and old rows may retain nullable Run lineage.
For a legacy failed tree, pass its structural root ID to `run.retry`: historical
Run enrollment and creation of the new retry commit together without resetting
old nodes. Reused completed results in the new Run carry no inherited usage.
Sanitized legacy definitions with redacted inline credentials require a fresh
definition using a service-owned `credential_ref`.

Migrations 006 and 007 intentionally reject destructive downgrade. Rollback
requires a planned restore of the matching code and complete database backup;
do not drop accounting tables or clear migration history. If new consumption
has occurred after upgrade, preserve and reconcile those facts before a restore.
Never replay interrupted remote operations merely because the schema upgraded.
See [Run recovery](rest-api.md#durable-submission-and-recovery) and
[usage delivery](cost-governance.md#public-budget-and-usage-operations).

---

**Feature ID:** F-SM
**Priority:** P0
**Phase:** Phase 1 (0.20.0-alpha.1)
**Tech Design Reference:** Section 4.1

---

## Purpose

Replace DuckDB with SQLite as the default embedded storage engine. This eliminates the `duckdb-engine` third-party dependency (~80MB) and uses Python's built-in SQLite support. SQLite WAL mode provides adequate concurrent read performance for standalone deployments.

---

## File Changes

### New Files

**`src/apflow/core/storage/dialects/sqlite.py`**

SQLite dialect implementing the `DialectConfig` protocol.

```python
class SQLiteDialect:
    @staticmethod
    def normalize_data(data: Dict[str, Any]) -> Dict[str, Any]:
        """Serialize dicts/lists to JSON strings for TEXT storage."""
        ...

    @staticmethod
    def denormalize_data(data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse JSON strings back to Python objects."""
        ...

    @staticmethod
    def get_connection_string(path: str = ":memory:") -> str:
        """Generate connection string.
        Args:
            path: ":memory:" | "file:shared?mode=memory&cache=shared&uri=true" | "/path/to/db"
        Returns: "sqlite:///..."
        Raises: ValueError if path is empty
        """
        ...

    @staticmethod
    def get_engine_kwargs() -> Dict[str, Any]:
        """Return {"pool_pre_ping": True}."""
        ...

    @staticmethod
    def get_pragma_statements() -> list[str]:
        """Return PRAGMA statements for WAL mode and performance."""
        # Returns:
        # ["PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL",
        #  "PRAGMA cache_size=-64000", "PRAGMA foreign_keys=ON",
        #  "PRAGMA busy_timeout=5000"]
        ...
```

### Deleted Files

- `src/apflow/core/storage/dialects/duckdb.py`

### Modified Files

**`src/apflow/core/storage/dialects/registry.py`**

| Line | Before | After |
|---|---|---|
| 6 | `from apflow.core.storage.dialects.duckdb import DuckDBDialect` | `from apflow.core.storage.dialects.sqlite import SQLiteDialect` |
| 45 | `register_dialect("duckdb", DuckDBDialect)` | `register_dialect("sqlite", SQLiteDialect)` |
| 46 | `register_dialect("duckdb", DuckDBDialect)  # Alias` | Delete this line |

**`src/apflow/core/storage/factory.py`**

Changes:
1. Replace default dialect from `"duckdb"` to `"sqlite"` in `SessionPoolManager.__init__()` and `create_session()`.
2. Replace default connection string from `"duckdb:///:memory:"` to `"sqlite:///:memory:"` everywhere.
3. Add SQLite PRAGMA event listener after engine creation:

```python
from sqlalchemy import event

def _apply_sqlite_pragmas(engine: Engine) -> None:
    """Apply PRAGMA statements for WAL mode on SQLite connections."""
    if "sqlite" in str(engine.url):
        from apflow.core.storage.dialects.sqlite import SQLiteDialect

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            for pragma in SQLiteDialect.get_pragma_statements():
                cursor.execute(pragma)
            cursor.close()
```

4. Call `_apply_sqlite_pragmas(engine)` after every `create_engine()` or `create_async_engine()` call.

**`src/apflow/core/storage/migrations/001_add_task_tree_fields.py`**

Replace `information_schema` queries with `sqlalchemy.inspect()`:

```python
# Before:
result = conn.execute(text(
    f"SELECT COUNT(*) FROM information_schema.tables WHERE table_name = '{table_name}'"
))
table_exists = result.scalar() > 0

# After:
from sqlalchemy import inspect as sa_inspect
inspector = sa_inspect(engine)
table_exists = table_name in inspector.get_table_names()
```

Replace column existence checks:

```python
# Before (if using information_schema.columns):
# Check if column exists

# After:
existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
if "task_tree_id" not in existing_columns:
    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN task_tree_id VARCHAR(255)"))
```

**`src/apflow/core/storage/migrations/002_add_scheduling_fields.py`**

Same pattern: replace `information_schema` with `inspect()`.

**`src/apflow/core/storage/migrations/003_add_distributed_support.py`**

Same pattern: replace `information_schema` with `inspect()`.

**`pyproject.toml`**

Remove from `dependencies`:
```
"duckdb-engine>=0.10.0",
"pytz>=2024.1",
```

---

## Data Models

The original DuckDB-to-SQLite change was dialect-only. The 0.24.0 schema additions
are described in the upgrade section above.

---

## Test Requirements

### Unit Tests: `tests/core/storage/dialects/test_sqlite_dialect.py`

```python
def test_get_connection_string_memory():
    """':memory:' returns 'sqlite:///:memory:'."""
    assert SQLiteDialect.get_connection_string(":memory:") == "sqlite:///:memory:"

def test_get_connection_string_shared_memory():
    """Shared memory URI passes through correctly."""
    path = "file:shared?mode=memory&cache=shared&uri=true"
    result = SQLiteDialect.get_connection_string(path)
    assert result == f"sqlite:///{path}"

def test_get_connection_string_file():
    """File path is resolved to absolute."""
    result = SQLiteDialect.get_connection_string("apflow.db")
    assert result.startswith("sqlite:///")
    assert result.endswith("apflow.db")
    assert "/" in result  # absolute path

def test_get_connection_string_empty_raises():
    """Empty path raises ValueError."""
    with pytest.raises(ValueError, match="must not be empty"):
        SQLiteDialect.get_connection_string("")

def test_normalize_data_dict():
    """Dicts are JSON-serialized."""
    result = SQLiteDialect.normalize_data({"config": {"key": "val"}})
    assert result["config"] == '{"key": "val"}'

def test_normalize_data_list():
    """Lists are JSON-serialized."""
    result = SQLiteDialect.normalize_data({"items": [1, 2, 3]})
    assert result["items"] == "[1, 2, 3]"

def test_normalize_data_scalar():
    """Scalars pass through unchanged."""
    result = SQLiteDialect.normalize_data({"name": "test", "count": 5})
    assert result == {"name": "test", "count": 5}

def test_denormalize_data_roundtrip():
    """normalize -> denormalize preserves original data."""
    original = {"config": {"nested": True}, "name": "test"}
    normalized = SQLiteDialect.normalize_data(original)
    denormalized = SQLiteDialect.denormalize_data(normalized)
    assert denormalized == original

def test_get_pragma_statements():
    """Returns expected PRAGMA list."""
    pragmas = SQLiteDialect.get_pragma_statements()
    assert any("journal_mode=WAL" in p for p in pragmas)
    assert any("foreign_keys=ON" in p for p in pragmas)
    assert any("busy_timeout" in p for p in pragmas)
    assert len(pragmas) == 5
```

### Integration Tests: `tests/core/storage/test_sqlite_session.py`

```python
async def test_create_session_sqlite_memory():
    """Create session with SQLite memory mode."""
    session = create_session(dialect="sqlite", connection_string="sqlite:///:memory:")
    assert session is not None

async def test_create_and_query_task_sqlite():
    """Create a task and query it back using SQLite."""
    session = create_session(dialect="sqlite", connection_string="sqlite:///:memory:")
    repo = TaskRepository(session)
    # Insert task, query back, verify fields

async def test_sqlite_wal_mode_file():
    """File-based SQLite has WAL mode enabled."""
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        session = create_session(dialect="sqlite", connection_string=f"sqlite:///{db_path}")
        # Verify WAL mode
        result = session.execute(text("PRAGMA journal_mode"))
        assert result.scalar() == "wal"
    finally:
        os.unlink(db_path)

async def test_migration_001_sqlite():
    """Migration 001 applies cleanly on SQLite."""
    # Fresh SQLite DB, run migration, verify columns added

async def test_migration_002_sqlite():
    """Migration 002 applies cleanly on SQLite."""

async def test_migration_003_sqlite():
    """Migration 003 applies cleanly on SQLite."""
```

---

## Acceptance Criteria

1. `pip install apflow` does not install `duckdb-engine`.
2. `create_session()` defaults to SQLite and works without any configuration.
3. `create_session(dialect="sqlite", connection_string="sqlite:///:memory:")` works for testing.
4. `create_session(dialect="sqlite", connection_string="sqlite:///apflow.db")` creates a file-based DB with WAL mode.
5. All existing migrations (001-003) apply cleanly on SQLite.
6. All preserved unit tests pass with SQLite backend.
7. `create_session(dialect="postgresql", ...)` continues to work for distributed deployments.
