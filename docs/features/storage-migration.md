# Feature Spec: Storage Migration (DuckDB to SQLite)

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

No data model changes. This is a dialect-level change only.

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
