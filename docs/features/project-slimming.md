# Feature Spec: Project Slimming (F-001)

**Feature ID:** F-001
**Priority:** P0
**Phase:** Phase 1 (0.20.0-alpha.1)
**Tech Design Reference:** Section 4.2

---

## Purpose

Remove all self-built protocol layers, CLI, and extensions that are superseded by the apcore ecosystem or out of scope for v2. Reduces the codebase from ~48,500 lines to ~31,000 lines and eliminates 13 third-party dependency groups.

---

## File Changes

### Directories to Delete (Complete List)

```
src/apflow/api/                    # Entire directory (a2a, mcp, graphql, docs, main.py)
src/apflow/cli/                    # Entire directory (18 files)
src/apflow/extensions/crewai/      # Entire directory (4 files)
src/apflow/extensions/llm/         # Entire directory (2 files)
src/apflow/extensions/generate/    # Entire directory (8 files)
src/apflow/extensions/grpc/        # Entire directory (2 files)
src/apflow/extensions/tools/       # Entire directory (3 files)
```

### Test Directories to Delete

```
tests/api/                         # All API tests
tests/cli/                         # All CLI tests
tests/extensions/crewai/           # All CrewAI tests
tests/extensions/llm/              # All LLM tests
tests/extensions/generate/         # All generate tests
tests/extensions/grpc/             # All gRPC tests
tests/extensions/tools/            # All tools tests
```

### Modified Files

**`pyproject.toml`**

Step-by-step changes:

1. Update `version` from `"0.18.2"` to `"0.20.0a1"`.
2. Update `description` from `"Agent workflow orchestration and execution platform"` to `"AI Agent Production Middleware"`.
3. Update `requires-python` from `">=3.10"` to `">=3.11"`.
4. Remove from `classifiers`: `"Programming Language :: Python :: 3.10"`.
5. In `dependencies`, remove: `"duckdb-engine>=0.10.0"`, `"pytz>=2024.1"`.
6. In `dependencies`, add: `"apcore>=0.14.0"`.
7. Delete these `[project.optional-dependencies]` sections entirely:
   - `a2a` (a2a-sdk, fastapi, uvicorn, starlette, websockets, httpx, aiohttp, python-jose)
   - `cli` (click, rich, typer, python-dotenv, nest_asyncio, httpx, PyJWT, pyyaml)
   - `crewai` (crewai, litellm, anthropic, aiodns)
   - `llm` (litellm)
   - `grpc` (grpclib, protobuf)
   - `graphql` (strawberry-graphql, fastapi, starlette, uvicorn)
   - `tools` (requests, beautifulsoup4, trafilatura, brotli, bs4)
   - `llm-key-config` (empty)
   - `mcp` (empty, comments only)
   - `standard` (meta extra referencing deleted extras)
   - `all` (meta extra referencing deleted extras)
8. Add new `[project.optional-dependencies]` sections:
   ```toml
   mcp-server = ["apcore-mcp>=0.10.1"]
   a2a-server = ["apcore-a2a"]
   cli-gen = ["apcore-cli>=0.3.0"]
   ```
9. In `dev`, remove: `"apdev[dev]>=0.1.6"`, `"jsonfinder>=0.4.0"`, `"memory-profiler>=0.61.0"`, `"psutil>=5.9.0"`.
10. Delete `[project.scripts]` section entirely (removes `apflow` and `apflow-server` entry points).
11. Update `[tool.black]` `target-version` to `['py311', 'py312']`.
12. Update `[tool.ruff]` `target-version` to `"py311"`.
13. Update `[tool.mypy]` `python_version` to `"3.11"`.

**`src/apflow/__init__.py`**

1. Update `__version__` to `"0.20.0a1"`.
2. Update module docstring:
   ```python
   """
   apflow - AI Agent Production Middleware

   Framework-agnostic production middleware that makes AI agents reliable,
   cost-governed, and auditable.

   Core modules (always included):
   - core.interfaces: Core interfaces (ExecutableTask, BaseTask)
   - core.execution: Task orchestration (TaskManager, StreamingCallbacks)
   - core.extensions: Unified extension system (ExtensionRegistry)
   - core.storage: Database session factory (SQLite default, PostgreSQL optional)
   - bridge: apcore Module registration
   - durability: Checkpoint/resume, retry, circuit breaker
   - governance: Token budget, cost policy, usage reporting
   """
   ```
3. Remove deprecated backward-compatibility exports from `__all__`: `"create_storage"`, `"get_default_storage"`.
4. Remove the lazy import branch for `create_storage` and `get_default_storage` in `__getattr__`.

### Import Reference Cleanup

After deletion, search for and fix any remaining references to deleted modules:

**Search pattern: `from apflow.extensions.crewai`**
- Location: `src/apflow/core/execution/task_executor.py` (lazy import for CrewAI executor type detection)
- Action: Remove the lazy import block. The executor registry handles type resolution; no special-casing needed.

**Search pattern: `from apflow.api`**
- Location: Possibly in `src/apflow/__init__.py` or test conftest files
- Action: Remove any references found.

**Search pattern: `from apflow.cli`**
- Location: Possibly in test conftest files
- Action: Remove any references found.

**Search pattern: `crewai` in `pyproject.toml`**
- Action: Ensure no remaining references in extras or dependency lists.

**Search pattern: `duckdb` anywhere in `src/`**
- Action: Remove references in storage code (handled by storage-migration.md), documentation comments, and any config defaults.

---

## Method Signatures

No new methods. This feature is purely subtractive.

---

## Data Models

No data model changes.

---

## Test Requirements

### Pre-Deletion Baseline

Before any deletions, run the full test suite and record:
- Total test count
- Pass count
- Failure count
- Test duration

### Post-Deletion Verification

After all deletions:

```python
def test_preserved_imports():
    """All preserved public imports still work."""
    from apflow import ExecutableTask, BaseTask, TaskManager
    from apflow import StreamingCallbacks, create_session
    from apflow import executor_register, storage_register, hook_register
    from apflow.core.execution.task_creator import TaskCreator
    from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
    from apflow.core.extensions.scanner import ExtensionScanner

def test_deleted_imports_fail():
    """Deleted module imports raise ImportError."""
    with pytest.raises(ImportError):
        from apflow.api.a2a import agent
    with pytest.raises(ImportError):
        from apflow.cli.main import main
    with pytest.raises(ImportError):
        from apflow.extensions.crewai import CrewaiExecutor
    with pytest.raises(ImportError):
        from apflow.extensions.generate import GenerateExecutor
    with pytest.raises(ImportError):
        from apflow.extensions.grpc import GrpcExecutor

def test_pip_install_succeeds():
    """pip install apflow succeeds with only core + apcore deps."""
    # Verify via subprocess: pip install . --dry-run
    # Check no duckdb-engine, no crewai, no fastapi in resolved deps

def test_remaining_tests_pass():
    """All preserved test files pass."""
    # Run: pytest tests/ --ignore=tests/api --ignore=tests/cli ...
    # Verify exit code 0
```

---

## Acceptance Criteria

1. All listed directories are deleted from the source tree.
2. `pyproject.toml` has no references to deleted modules or their dependencies.
3. `pip install apflow` succeeds with only core + apcore dependencies.
4. `pip install apflow` does not install: duckdb-engine, crewai, litellm, fastapi, uvicorn, click, rich, typer, strawberry-graphql, grpclib, protobuf, beautifulsoup4, trafilatura.
5. Remaining test suite passes (tests for deleted modules are also removed).
6. No `from apflow.api`, `from apflow.cli`, `from apflow.extensions.crewai`, `from apflow.extensions.llm`, `from apflow.extensions.generate`, `from apflow.extensions.grpc`, or `from apflow.extensions.tools` imports exist in preserved source code.
7. `__version__` is `"0.20.0a1"`.
8. `project.scripts` section is removed.
