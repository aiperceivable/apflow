---
description: Feature specification for slimming apflow by removing obsolete modules and relying on the apcore ecosystem.
---

# Feature Spec: Project Slimming (F-001)

**Feature ID:** F-001
**Priority:** P0
**Phase:** Phase 1 (0.20.0-alpha.1)
**Tech Design Reference:** Section 4.2

---

## Purpose

Remove legacy self-built protocol layers, the old standalone CLI implementation,
and extensions that are superseded by the apcore ecosystem or out of scope for
v2. The current source keeps thin API and CLI surfaces backed by apcore protocol
adapters.

---

## File Changes

### Directories to Delete (Complete List)

```
src/apflow/api/a2a/                # Legacy self-built A2A package
src/apflow/api/mcp/                # Legacy self-built MCP package
src/apflow/api/graphql/            # Legacy GraphQL package
src/apflow/api/docs/               # Legacy docs UI package
src/apflow/cli/                    # Legacy multi-file CLI package
src/apflow/extensions/crewai/      # Entire directory (4 files)
src/apflow/extensions/llm/         # Entire directory (2 files)
src/apflow/extensions/generate/    # Entire directory (8 files)
src/apflow/extensions/grpc/        # Entire directory (2 files)
src/apflow/extensions/tools/       # Entire directory (3 files)
```

### Test Directories to Delete

```
tests/api/a2a/                     # Legacy self-built A2A tests
tests/api/mcp/                     # Legacy self-built MCP tests
tests/api/graphql/                 # Legacy GraphQL tests
tests/cli/legacy/                  # Legacy multi-file CLI tests
tests/extensions/crewai/           # All CrewAI tests
tests/extensions/llm/              # All LLM tests
tests/extensions/generate/         # All generate tests
tests/extensions/grpc/             # All gRPC tests
tests/extensions/tools/            # All tools tests
```

### Modified Files

**`pyproject.toml`**

Step-by-step changes:

1. Update package `version` for the target release.
2. Update `description` to `"AI-Perceivable Distributed Orchestration"`.
3. Update `requires-python` from `">=3.10"` to `">=3.11"`.
4. Remove from `classifiers`: `"Programming Language :: Python :: 3.10"`.
5. In `dependencies`, remove: `"duckdb-engine>=0.10.0"`, `"pytz>=2024.1"`.
6. In `dependencies`, add: `"apcore>=0.14.0"`.
7. Delete these `[project.optional-dependencies]` sections entirely:
   - `a2a` (a2a-sdk, fastapi, uvicorn, starlette, websockets, httpx, aiohttp, python-jose)
   - `cli` (legacy CLI extra; the current thin CLI keeps `click` as a core dependency)
   - `crewai` (crewai, litellm, anthropic, aiodns)
   - `llm` (litellm)
   - `grpc` (grpclib, protobuf)
   - `graphql` (strawberry-graphql, fastapi, starlette, uvicorn)
   - `tools` (requests, beautifulsoup4, trafilatura, brotli, bs4)
   - `llm-key-config` (empty)
   - `mcp` (empty, comments only)
   - `standard` (meta extra referencing deleted extras)
   - old `all` definition if it references deleted extras
8. Add `apcore-mcp>=0.11.0`, `apcore-a2a>=0.3.0`, `apcore-cli>=0.3.0` to core `dependencies` (not optional — MCP, A2A, CLI are standard features of apflow).
9. In `dev`, remove: `"apdev[dev]>=0.1.6"`, `"jsonfinder>=0.4.0"`, `"memory-profiler>=0.61.0"`, `"psutil>=5.9.0"`.
10. Preserve `[project.scripts] apflow = "apflow.cli:main"` as the supported CLI entry point; remove only obsolete script entries such as `apflow-server`.
11. Update `[tool.black]` `target-version` to `['py311', 'py312']`.
12. Update `[tool.ruff]` `target-version` to `"py311"`.
13. Update `[tool.mypy]` `python_version` to `"3.11"`.

**`src/apflow/__init__.py`**

1. Keep `__version__` aligned with the package version in `pyproject.toml`.
2. Update module docstring:
   ```python
   """
   apflow - AI-Perceivable Distributed Orchestration

   A distributed task orchestration engine where every capability is
   AI-perceivable via the apcore module standard.

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

**Search pattern: `from apflow.api.a2a`, `from apflow.api.mcp`, `from apflow.api.graphql`**
- Location: Possibly in test conftest files or deleted legacy tests.
- Action: Remove references to deleted legacy protocol packages. Keep imports from
  the current `apflow.api` REST/combined server package.

**Search pattern: `from apflow.cli.main`**
- Location: Possibly in legacy CLI tests.
- Action: Remove references to the deleted legacy CLI package. Keep imports from
  the current `apflow.cli` module.

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
4. `pip install apflow` does not install removed optional integration dependencies such as duckdb-engine, crewai, litellm, rich, typer, strawberry-graphql, grpclib, protobuf, beautifulsoup4, or trafilatura. The thin CLI remains part of core, so `click` is expected; HTTP server dependencies may arrive transitively through apcore protocol adapters.
5. Remaining test suite passes (tests for deleted modules are also removed).
6. No `from apflow.extensions.crewai`, `from apflow.extensions.llm`, `from apflow.extensions.generate`, `from apflow.extensions.grpc`, or `from apflow.extensions.tools` imports exist in preserved source code.
7. `__version__` matches the package version in `pyproject.toml`.
8. The `apflow` console script is preserved as the supported CLI entry point.
