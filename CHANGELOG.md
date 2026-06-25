# Changelog


## [0.21.0] - 2026-06-25

### Added

- **apcore ecosystem upgrade — 0.22.0 → 0.25.0 coordinated release**
  - Dependency floors: `apcore>=0.25.0`, `apcore-toolkit>=0.9.1`, `apcore-mcp>=0.17.0`,
    `apcore-a2a>=0.4.2`, `apcore-cli>=0.10.2`
  - **A2A protocol 1.0** (apcore-a2a 0.4.0): protobuf types, `Task` event before status
    updates, `AgentCard.supported_interfaces`, `/.well-known/agent-card.json` canonical path
  - **Per-instance `ToggleState` isolation** (apcore 0.24.0): disabling a module on one
    `APCore` instance no longer bleeds into other instances
  - **AI error-recovery metadata** (apcore 0.23.0): `user_fixable` and `ai_guidance` are
    auto-populated on `ModuleError` at construction time — no code change required
  - **Config-driven ACL discovery** (apcore 0.25.0): `ACL.discover(config)` auto-wired in
    `APCore.__init__`; skipped when caller supplies its own Executor (no behavior change for
    apflow's current usage)
  - **`serve()` Phase B fix** (apcore-mcp 0.17.0): `approval_store` and `approval_notify`
    are now correctly forwarded through `serve()` / `async_serve()` (was broken in 0.16.0)
  - **apcore-cli 0.10**: `create_cli()` uses the SDK's native `version=` / `description=`
    parameters — the SDK version no longer leaks into the help text

- **`ModuleAnnotations` on all 16 bridge task modules** — correct `readonly`, `destructive`,
  `requires_approval`, `idempotent`, and `paginated` flags so MCP/A2A clients and AI agents
  receive accurate capability metadata for every orchestration tool

- **Module preview support** (`__apcore_module_preview` meta-tool)
  - `ExecutableTaskModuleAdapter.preview()`: describes which executor would run
  - `TaskExecuteModule.preview()`: names the task that would be triggered
  - `TaskDeleteModule.preview()`: names the task that would be permanently deleted
  - `TaskCancelModule.preview()`: lists every task that would be cancelled
  - AI agents can call `__apcore_module_preview` to inspect the impact of a destructive
    operation before confirming it

- **`apflow mcp --approval`** — opt-in async human-approval workflow (apcore-mcp Phase B).
  Registers the `__apcore_approval_check` meta-tool so AI agents can poll for out-of-band
  approvals without blocking the MCP connection. Uses `InMemoryApprovalStore` (dev only;
  production deployments wire a persistent `ApprovalStore` via `APCoreMCP` directly).

- **`apflow serve --sys-modules`** — expose apcore `sys.*` introspection modules as A2A
  skills (opt-in; off by default)

- **`apflow mcp --metrics`** — opt-in Prometheus metrics + usage middleware (wires
  `observability=True` on apcore-mcp 0.15+)

- **`DistributedRuntime.from_session()`** construction seam — derives a `sessionmaker` from
  a session's bound engine and builds the runtime with its full correct argument contract,
  eliminating caller-side argument reconstruction bugs across `create_app()` and `cli worker`

### Fixed

#### Distributed coordinator
- **`apflow worker` crash** — `WorkerRuntime` was called with 1 of 7 required positional
  args (TypeError). Now constructs via `from_session()`.
- **`create_app(cluster=True)` silent no-op** — `DistributedRuntime()` received missing
  `session_factory` (TypeError swallowed by a bare `except`), making cluster mode silently
  degrade to single-node. Now raises an explicit `ValueError` when the database is not
  PostgreSQL.
- **`is_leader` side-effects** — `DistributedRuntime.is_leader` was mutating `_role` /
  `_lease_token` on expiry reads; reading it from a status endpoint could silently demote
  the node. Now a pure read; demotion is owned solely by `_leader_renewal_loop`. (group E)
- **Lease-gated status write** — `_finalize_task` gates the status write on the token-scoped
  lease `DELETE` matching a row; stale workers can no longer clobber the new owner's status
  after lease-expiry reassignment. (group D)
- **Duplicate task poll** — `_poll_loop` skips tasks already in `_running_tasks`, preventing
  still-pending rows from being re-spawned each cycle and overwriting the live entry.
  (group D)
- **Lease leak on cancel** — `_execute_task` releases the lease on `CancelledError` before
  `finally` pops `_active_leases`, so shutdown's release loop no longer misses in-flight
  leases. (group D)
- **Idempotency key correctness** — now incorporates task inputs (per the documented
  contract); computed once and shared across the cache-check / store / failure paths.
  `cleanup_expired_leases` bumps `attempt_id` when reverting `in_progress→pending` so
  reassigned attempts compute a fresh key instead of colliding. (group D)
- **`store_result` conflict** — reconciles a stale `failed` row to `completed` rather than
  treating every key conflict as success. (group D)
- **`NodeRegistry` `DetachedInstanceError`** — `detect_stale_nodes` / `detect_dead_nodes` /
  `get_healthy_nodes` now call `session.expunge_all()` before returning so ORM objects are
  detached while their attributes are loaded, matching `worker._find_executable_tasks`.
  (group E)
- **Cluster bootstrap PostgreSQL guard** — `create_app(cluster=True)` calls `is_postgresql()`
  and raises explicitly instead of silently continuing with SQLite. (group A)
- **`DistributedConfig` timing invariant** — `validate_and_initialize()` now rejects
  `leader_renew_seconds >= leader_lease_seconds`, which caused silent leader flapping.

#### TaskManager
- **False failure classification** — `_handle_task_execution_result` treated any result with
  an `"error"` key (even `None` / `""`) as a failure. Now only fails on a truthy error
  value. (group F)
- **Parallel branch cancellation** — `_execute_priority_group` now uses
  `return_exceptions=True` so one branch's failure no longer cancels in-flight siblings
  (which stranded them in `in_progress`). The first real failure is still re-raised to
  preserve the fail-the-tree contract. (group F)
- **Credentials / PII in logs** — demoted two `INFO` logs that dumped raw task inputs /
  results to `DEBUG`. (group F)

#### Bridge / task modules
- **`reset_kwargs` mutation** — `TaskCreator._clone_task` operated on one shared dict across
  a whole tree clone; a task's `id` / lineage fields were written back and corrupted later
  clones. Now operates on a per-call copy. (group C)
- **Mixed-mode `origin_type` bleed** — `_reset_task_recursive` mutated the shared override
  dict when applying `origin_type=link` to link nodes, silently turning subsequent sibling /
  descendant copy nodes into links (`convert_to_link` nulled their data). Now derives a
  fresh per-node dict. (group C)
- **`TaskCancelModule` iterating string** — when `task_ids` was a bare string, `cancel_task`
  was called once per character. Now raises `ValueError("task_ids must be an array")`.
  (group B)
- **Override allowlist** — `TaskCopyModule` / `TaskLinkModule` / `TaskCloneMixedModule` now
  filter external agent overrides through a content-only allowlist (`name`, `inputs`,
  `params`, `priority`). Prevents an agent from splatting `user_id` / `task_tree_id` /
  `origin_type` / `status` to corrupt tenancy or tree-lineage invariants. (group B)
- **`TaskScheduledListModule` unbounded results** — now queries via `get_scheduled_tasks()`
  with DB-level `user_id` scoping and ordered `LIMIT`; limit clamped to 100. Previously
  fetched all tasks then sliced in Python. (group B)
- **`TaskRunningListModule` limit bypass** — limit clamp now caps at the advertised schema
  maximum (100) instead of 1000. (group B)
- **`limit` / `offset` coercion** — all list modules now run inputs through `_coerce_int()`
  so non-numeric / null MCP inputs raise a clean `ValueError` instead of crashing in
  `min()` / `max()`. Non-object elements in `tasks[]` array raise `ValueError` instead of
  `AttributeError`. `TaskUpdate` writes only schema-advertised fields via a whitelist and
  no longer mutates the caller's inputs dict.

### Removed

- **Dead `ExecutorRegistry` class** — `src/apflow/core/execution/executor_registry.py` had
  zero production callers (live path uses `apflow.core.extensions.get_registry`). Removed
  along with its lazy re-exports from `core/execution/__init__.py` and the dead `conftest`
  reset hook. (group G)

### Changed

- **`ExtensionRegistry.register()`** — returns `None` on the skip path (matching its
  `-> None` annotation); docstring corrected (it warns and skips on duplicate ID, not
  raises). (group G)
- **`scanner.scan_builtin_executors()`** — clears the in-memory metadata cache before a
  rescan so deleted / renamed executors no longer linger. (group G)
- **`scanner_bridge`** — bare `except: pass` around entry-point group discovery replaced
  with a logged warning; broken plugin metadata is now observable. (group G)
- **`StreamingCallbacks`** — retains strong references to fire-and-forget progress tasks (in
  a set, discarded on completion) to prevent garbage collection mid-flight. (group G)
- **Version**: 0.20.0 → 0.21.0

---

## [0.20.0] - 2026-04-16

### BREAKING CHANGES — Product Repositioning

apflow v2 is repositioned from "task orchestration framework" to **AI Agent Production Middleware**. This is a major rewrite. The v1 codebase is preserved on the `v1-task-orchestration` branch.

### Removed

Stripped down to 4 core executors. The remaining executors are examples; real executors are AI agents, business logic, or any `ExecutableTask` implementation.

- **Kept**: `RestExecutor`, `SendEmailExecutor`, `AggregateResultsExecutor`, `ApFlowApiExecutor`
- **Deleted**: `SshExecutor`, `DockerExecutor`, `ScrapeExecutor`, `WebSocketExecutor`, `McpExecutor`, `CommandExecutor`, `SystemInfoExecutor` (the latter two lived in `extensions/stdio/`)
- **Deleted**: `core/tools/` framework (`BaseTool`, `ToolRegistry` — dead code, no consumers)
- **Deleted**: `extensions/llm_key_config/` — dead code, no importers
- **Removed extras** from `pyproject.toml`: `ssh`, `docker`
- **Self-built protocol layers**: `api/a2a/`, `api/mcp/`, `api/graphql/`, `api/docs/`, `api/routes/` — replaced by apcore ecosystem
- **CLI**: `cli/` — replaced by apcore-cli
- **AI framework wrappers**: `extensions/crewai/`, `extensions/llm/`, `extensions/generate/` — out of scope for middleware
- **Other executors**: `extensions/grpc/`, `extensions/tools/` — dropped
- **DuckDB storage**: replaced by SQLite
- **Dependencies removed**: duckdb-engine, pytz, fastapi, uvicorn, a2a-sdk, rich, typer, crewai, litellm, anthropic, strawberry-graphql, grpclib, protobuf, beautifulsoup4, trafilatura
- **Entry points removed**: `apflow-server` command (replaced by `apflow serve`)
- **25+ stale v1 documentation files**
- **Deprecated backward-compatibility aliases**: `create_storage()` and `get_default_storage()` removed from `core/storage/factory.py` and `core/storage/__init__.py` — use `create_session()` and `get_default_session()` instead
- **Deprecated logger shim**: `apflow.core.utils.logger` module deleted — use `from apflow.logger import get_logger` directly
- **Deprecated extension constants**: `EXTENSION_CONFIG` and `EXECUTOR_ID_TO_EXTENSION` removed from `core/extensions/manager.py` — extensions are auto-discovered via `ExtensionScanner`
- **Test backward-compatibility aliases**: `create_storage` / `get_default_storage` aliases removed from `tests/conftest.py`

### Added
- `@function_executor` decorator for lightweight executor registration (2642b04)
- Atomic compare-and-swap task claiming to prevent concurrency races (ce845d5)
- Restored 7 task lifecycle tools (link/copy/archive) exposed via MCP/A2A/CLI (5ab65a9, 171199f)
- Expanded `ExtensionScanner` test suite (a1519b8, bfea353)
- `task.create_tree` MCP tool — AI agents create entire task trees in a single call (a20f226)
- Distributed wiring in CLI — `apflow serve --cluster`, `apflow worker` (a20f226)
- End-to-end example `examples/quickstart.py` — register → create tree → execute → get results (a20f226)
- **apcore Module Bridge** (`bridge/`)
  - `ExecutableTaskModuleAdapter`: wraps any executor as apcore Module
  - `discover_executor_modules()`: auto-discovers executors via AST scanner
  - 5 task management modules: create, execute, list, get, delete
  - `create_apflow_registry()`: one call to register all apflow capabilities
  - Zero config: `pip install apflow` → MCP + A2A + CLI ready

- **Durable Execution** (`durability/`)
  - `CheckpointManager`: save/load/delete execution state in `task_checkpoints` table
  - `RetryManager`: fixed/exponential/linear backoff with jitter
  - `CircuitBreaker`: CLOSED/OPEN/HALF_OPEN state machine per executor, thread-safe
  - `CircuitBreakerRegistry`: thread-safe registry of per-executor breakers
  - `ExecutableTask` interface: 3 new optional methods (`supports_checkpoint`, `get_checkpoint`, `resume_from_checkpoint`)
  - Full TaskManager integration: circuit breaker pre-check, checkpoint loading, retry wrapping, post-execution cleanup

- **Cost Governance** (`governance/`)
  - `BudgetManager`: per-task token budget tracking with check/update
  - `PolicyEngine`: register named policies with block/downgrade/notify/continue actions
  - `ProviderRouter`: model downgrade chains (e.g., opus → sonnet → haiku)
  - `UsageReporter`: aggregated usage reporting with JSON export
  - Full TaskManager integration: pre-execution budget check with policy evaluation, post-execution token usage update

- **SQLite Storage** (replacing DuckDB)
  - `SQLiteDialect`: WAL mode, 5 PRAGMAs for optimal performance
  - Memory mode (`sqlite:///:memory:`) for testing
  - File mode with WAL for standalone production
  - Zero external dependencies (Python stdlib)
  - All 3 existing migrations ported from `information_schema` to `sqlalchemy.inspect()`

- **Database Migration 004**: durability fields (6) + governance fields (5) + `task_checkpoints` table

- **TaskModel extensions**: 11 new fields (checkpoint_at, resume_from, attempt_count, max_attempts, backoff_strategy, backoff_base_seconds, token_usage, token_budget, estimated_cost_usd, actual_cost_usd, cost_policy)

- **TASK_TABLE_NAME validation**: regex guard against SQL injection from environment variable

- **CLI entry points** (`cli.py` + `app.py`)
  - `apflow serve`: Start A2A HTTP server with optional Explorer UI
  - `apflow mcp`: Start MCP server (stdio, streamable-http, or SSE)
  - `apflow info`: Show version, configuration, and registered modules
  - `create_app()`: Factory function bootstraps full stack with durability + governance

- **ConfigManager with YAML support** (`config_manager.py`)
  - Load from: defaults → `apflow.yaml` → environment variables
  - Env override: `api.server_url` → `APFLOW_API_SERVER_URL`
  - Type coercion for int/float/bool/list from env strings
  - `get(key, default)` interface for future apcore Config migration

### Fixed

- **Concurrent child completion race conditions** (`core/execution/task_manager.py`, `core/storage/sqlalchemy/task_repository.py`)
  - **TOCTOU double-trigger**: When multiple child tasks completed concurrently via `asyncio.gather`, two coroutines could both pass the `in_progress` status check and execute the parent task twice. Fixed by replacing the non-atomic check-then-set with `TaskRepository.try_claim_for_execution()` — a conditional `UPDATE ... WHERE status IN (...)` that guarantees exactly one caller wins the claim.
  - **Orphaned parent on exception**: If `execute_after_task` threw an exception for the last completing child (e.g. transient DB error), the parent task would be permanently stuck in `pending` with no future callback to re-check. Fixed by adding a 3-attempt retry with exponential backoff (0.1s, 0.2s) around `execute_after_task` in `_handle_task_execution_result`.
  - **Retry was dead code**: The broad `try/except` inside `execute_after_task` swallowed all exceptions, making the caller's retry loop ineffective. Fixed by splitting `execute_after_task` into two phases: Phase 1 (post-hooks) isolates errors internally; Phase 2 (dependency check & trigger) lets exceptions propagate for retry.
  - **Stale ORM cache**: Added `expire_all()` before the dependency tree query in `execute_after_task` so concurrent coroutines see the latest committed child statuses.
  - **False streaming event**: Moved `task_start` callback to fire after successful atomic claim, preventing false "task started" events when the claim is rejected.
  - Type annotations: replace `Any` with typed imports across `app.py` and `task_manager.py` (Session, TaskManager, CheckpointManager, RetryManager, CircuitBreakerRegistry, BudgetManager, PolicyEngine) (8ee5040)
- `cli.py` stale positioning string corrected (8ee5040)
- `ExecutableTask` docstring updated to list the current 4 executors and mention `@function_executor` (8ee5040)

### Changed

- **Version**: 0.18.2 → 0.20.0
- **Python**: >= 3.10 → >= 3.11 (aligned with apcore)
- **Positioning**: "Distributed Task Orchestration Framework" → "AI-Perceivable Distributed Orchestration"
- **Core dependencies**: added apcore >= 0.14.0, apcore-mcp >= 0.11.0, apcore-a2a >= 0.3.0, apcore-cli >= 0.3.0
- **TaskCreator**: relaxed single-root constraint, allows multi-root task forests
- **README.md**: completely rewritten for v2
- **CLAUDE.md**: updated with v2 architecture overview
- Documentation restored for v2: Task Orchestration Architecture, Plant Analogy, dual-model (Structure Tree + Execution DAG)
- CLAUDE.md repositioned: apflow is **AI-perceivable**, not "NOT an AI product"
- Architecture diagrams corrected: apcore is embedded, not a middle layer
- Removed deprecated backward-compatibility aliases and stale code: `create_storage`/`get_default_storage` aliases, `apflow.core.utils.logger` shim, `EXTENSION_CONFIG`/`EXECUTOR_ID_TO_EXTENSION` constants (8316642)

---

## [0.18.2] - 2026-03-22

### Changed
- Rebrand: aipartnerup → aiperceivable

## [0.18.1] 2026-02-18

### Added

- **Docker Support**
  - Docker Compose configuration (`docker-compose.yml`) for easy deployment
  - Docker image definition (`Dockerfile`) for containerized apflow
  - `.dockerignore` file for optimized image builds

- **Enhanced GraphQL Testing**
  - New `test_loaders.py` for GraphQL DataLoaders testing (task loading by ID and children)
  - Additional test coverage in `test_mutations.py` for execute_task mutation
  - Expanded `test_queries.py` with task tree and running tasks queries
  - Enhanced `test_schema.py` to verify TaskTreeType and TaskProgressEvent in schema
  - Added task progress subscriptions tests in `test_subscriptions.py`
  - Improved tests for distributed configuration in `test_distributed_config.py` and `test_leader_election.py`
  - Enhanced lease management tests with concurrent acquisition and validation

### Changed

- `pyproject.toml`: Updated `apdev[dev]` dependency to version 0.1.6 for improved compatibility
- Documentation workflow: Updated GitHub Actions versions for consistency
- Shared `make_task_dict()` helper function in `conftest.py` for consistent task dictionary creation in GraphQL tests

### Fixed

- **PostgreSQL Driver Selection**
  - Fixed `normalize_postgresql_url()` function to correctly select async/sync driver based on `async_mode` parameter
  - Ensures proper database connection handling for both sync and async contexts

- **Configuration Management**
  - Fixed `test_scheduler.py` to properly patch `load_cli_config` in fixtures, preventing local config loading during tests
  - Enhanced runtime tests to handle cases when no task executor is configured

- **Documentation Workflow**
  - Added `workflow_dispatch` trigger for manual documentation deployment
  - Fixed GitHub Actions workflow compatibility for documentation site generation

## [0.18.0] 2026-02-16

### Added

- **Distributed cluster support** (`apflow.core.distributed`)
  - Leader election with PostgreSQL advisory locks
  - Lease-based task ownership with automatic expiration and renewal
  - Node registry with heartbeat-based health monitoring
  - Task placement strategies: round-robin, least-loaded, random, affinity-based
  - Distributed worker with configurable concurrency and graceful shutdown
  - Distributed runtime orchestrating node lifecycle, leader election, and task distribution
  - Idempotency store for at-least-once delivery with deduplication
  - Event bus for inter-node communication (task assigned/completed/failed, node join/leave, leader change)
  - Database migration `003_add_distributed_support` adding node, lease, and idempotency tables
  - Configuration via `DistributedConfig` with environment variable support (`APFLOW_DISTRIBUTED_*`)

- **GraphQL API adapter** (`apflow.api.graphql`)
  - Strawberry GraphQL schema with queries, mutations, and subscriptions
  - Task CRUD operations: create, get, list, cancel, retry
  - Real-time task status subscriptions via WebSocket
  - GraphQL server mountable as FastAPI sub-application
  - New `[graphql]` optional dependency group (`strawberry-graphql>=0.220.0`)

- **Protocol abstraction layer** (`apflow.api.protocols`)
  - `ProtocolAdapter` base class for multi-protocol support
  - A2A protocol adapter (`apflow.api.a2a.protocol_adapter`)
  - MCP protocol adapter (`apflow.api.mcp.protocol_adapter`)
  - Unified protocol types (`apflow.api.protocol_types`)
  - Protocol discovery and capability reporting in `capabilities.py`
  - Refactored `app.py` to use protocol registry for dynamic protocol mounting

- **Documentation**
  - [GraphQL API guide](docs/api/graphql.md) with schema reference and usage examples
  - [Distributed cluster guide](docs/guides/distributed-cluster.md) covering setup, configuration, and operation
  - Reorganized architecture docs: `exception-handling.md` and `task-manager.md` moved to `docs/architecture/`
  - Consolidated documentation from `apflow-docs` repo into `apflow` for single-repo deployment
  - [Protocol specification](docs/protocol/) (10 files): overview, core concepts, data model, execution lifecycle, examples, interfaces, conformance, errors, validation
  - Rich homepage (`docs/index.md`) with Material grid cards, learning paths, and quick navigation
  - Site assets: logo (`docs/assets/logo.svg`) and favicon (`docs/assets/favicon.ico`)
  - MkDocs configuration (`mkdocs.yml`) with Material theme, mermaid2 diagrams, code highlighting, and explicit nav structure
  - GitHub Actions workflow (`.github/workflows/docs.yml`) for automatic GitHub Pages deployment on push to `main`
  - New `[docs]` optional dependency group: `mkdocs`, `mkdocs-material`, `mkdocs-mermaid2-plugin`, `mkdocs-minify-plugin`, `pymdown-extensions`

### Changed

- `pyproject.toml`: Added `graphql` optional dependency group, added `greenlet>=3.0.0` to `postgres` extras, added `apdev[dev]>=0.1.2` to dev dependencies, included `graphql` in `[all]` extras, added `[docs]` optional dependency group
- `docs/README.md`: Simplified to point to the live documentation site with local development instructions
- `.gitignore`: Added `site/` for MkDocs build output
- `api/app.py`: Refactored to protocol registry pattern for mounting A2A, MCP, and GraphQL adapters
- `api/main.py`: Updated startup to support distributed mode with `--distributed` flag
- `core/execution/task_executor.py`: Added distributed-aware task execution with lease acquisition
- `core/storage/sqlalchemy/models.py`: Extended with distributed node, lease, and idempotency models
- `core/config/registry.py`: Added distributed configuration support

### Fixed

- Test mocking in `test_multi_phase_crew.py`: Added missing `crewai.Agent` and `crewai.Task` patches to prevent `OPENAI_API_KEY` errors in unit tests
- Integration test `test_scrape_crewai_chain_schema_based`: Added `api_keys_available` fixture to skip when API keys are not set

### Removed

- `scripts/` directory: Removed legacy utility scripts (`release.sh`, `check_allowed_chars.py`, `check_heavy_imports.py`, `check_import_performance.sh`, `detect_circular_imports.py`, `quick_import_check.py`)
- `docs/development/extending.md`: Removed in favor of reorganized architecture docs

## [0.17.0] 2026-02-12

### Added

- **CLI monitoring commands** (`apflow monitor`)
  - `apflow monitor status` - Display current task status with rich tables
  - `apflow monitor watch <task-id>` - Real-time task monitoring with live updates
  - `apflow monitor trace <task-id>` - Execution timeline with dependency tree visualization
  - `apflow monitor db` - Database connection status and statistics
  - Supports both API and local database modes
  - Rich terminal UI with tables, trees, panels, and live updates

- **Structured error handling with enhanced context**
  - `ApflowError` base class now supports structured format with `what`, `why`, `how_to_fix`, and `context` fields
  - Error messages include emoji indicators and formatted troubleshooting guidance
  - Backward compatible with existing error handling code
  - `NetworkError` and `AuthenticationError` classes with detailed context

- **Memory leak testing infrastructure**
  - Performance test suite in `tests/performance/test_memory_leaks.py`
  - Tests for memory stability over 1000+ task executions
  - Executor instance cleanup verification
  - Database file growth monitoring
  - Added `memory-profiler>=0.61.0` and `psutil>=5.9.0` to dev dependencies

- **Comprehensive documentation and examples**
  - **[Hello World Guide](docs/getting-started/hello-world.md)** - 5-minute quick start tutorial
  - **[Executor Selection Guide](docs/guides/executor-selection.md)** - Decision tree, comparison matrix, and detailed scenarios for all 8 executor types
  - Example code files in `examples/hello_world/`:
    - `simple.py` - Basic custom executor example
    - `with_dependencies.py` - Task dependencies example
    - `built_in_executors.py` - REST executor examples
  - Example code files in `examples/executor_examples/`:
    - `rest_executor_example.py` - HTTP API integration patterns
    - `ssh_executor_example.py` - Remote command execution examples
    - `docker_executor_example.py` - Container execution examples
  - Updated documentation indexes to reference new guides and examples

- **Enhanced error handling in executors**
  - REST executor: Improved exception handling for timeout, connection errors, SSL errors, and HTTP errors with structured context
  - SSH executor: Enhanced authentication error handling with key permission checks and troubleshooting guidance

### Changed

- **TaskRepository improvements**
  - Added `query_tasks_by_statuses()` method for efficient multi-status queries using SQLAlchemy IN clause
  - Optimized status filtering in monitoring commands

## [0.16.0] 2026-02-10

### Fixed

- **CLI `tasks scheduled complete` passing unsupported `result` parameter to repository**
  - `complete_scheduled_run()` does not accept a `result` parameter; removed the incorrect kwarg from the CLI direct-path call

- **CLI `tasks count` inefficient in-memory counting**
  - Replaced `query_tasks()` + `len()` per-status loop (which loaded all records into memory) with new `TaskRepository.count_tasks_by_status()` using `SELECT COUNT(*)` SQL queries, matching the API's efficient implementation

### Added

- **`--error-message` / `-m` option for `apflow tasks cancel`**
  - Allows specifying a custom cancellation error message, consistent with the API `error_message` parameter
  - Defaults to `"Cancelled by user"` / `"Force cancelled by user"` when not provided

- **`--calculate-next-run / --no-calculate-next-run` option for `apflow tasks scheduled complete`**
  - Controls whether `next_run_at` is recalculated after completing a scheduled run (default: `True`), consistent with the API `calculate_next_run` parameter

- **`--before` / `-b` option for `apflow tasks scheduled due`**
  - Allows specifying a custom cutoff time (ISO datetime) to query due tasks, consistent with the API `before` parameter
  - Defaults to current UTC time when not provided

- **`TaskRepository.count_tasks_by_status()` method**
  - Efficient SQL `COUNT(*)` grouped-by-status query with `user_id` and `root_only` filters

### Removed

- **`apflow tasks history` CLI command**
  - Removed non-functional stub command; underlying `TaskRepository.get_task_history()` and `TaskModel.status_log` were never implemented
  - Task history is managed at the A2A Protocol TaskStore layer, not TaskModel

### Changed

- Enhanced executor security and validation; added tests for JWT secret generation and TaskManager locking
- Updated token claims to use `roles` as a list; added tests for role-claim handling


## [0.15.0] 2026-02-08

### Fixed

- **Scheduler webhook trigger admin permission bypass**
  - `handle_webhook_trigger` now checks `_is_admin(request)` and sets `user_id=None` for admin users, matching the pattern used by all other handlers (`handle_tasks_list`, `handle_tasks_count`, etc.)
  - Previously, admin JWT tokens passed to the webhook trigger were treated as regular users, causing "Permission denied" when the scheduler triggered tasks owned by other users

- **Scheduled task re-execution not resetting child tasks**
  - `complete_scheduled_run()` now clears `result`, `progress`, and resets all child tasks to `pending` before the next scheduled run via `reset_task_tree_for_reexecution()`
  - Previously, a scheduled parent task would re-run but its children retained their `completed` status and stale results from the previous cycle, causing incorrect or skipped execution

- **ISO datetime 'Z' suffix parsing inconsistency**
  - Added `parse_iso_datetime()` helper in `core/utils/helpers.py` that normalises the `Z` suffix before calling `fromisoformat()`, fixing compatibility with Python 3.10 and below
  - Replaced scattered `datetime.fromisoformat(value.replace('Z', '+00:00'))` calls across `schedule_calculator.py`, `ical.py`, `tasks.py` routes, and CLI commands with the centralised helper

### Added

- **Scheduler auto-generates admin JWT for API access**
  - `InternalScheduler._get_auth_token()` auto-generates an admin JWT using the local `jwt_secret` from `config.cli.yaml` when `admin_auth_token` is not explicitly configured
  - Token is cached per scheduler session (generated once, reused for all API calls)
  - Falls back gracefully to unauthenticated access if JWT generation fails
  - Eliminates the need for manual token configuration when running `apflow scheduler start` locally

- **Scheduler startup auth identity logging**
  - `_log_auth_identity()` logs the auth source (configured token vs auto-generated) and JWT subject at startup for troubleshooting

- **Webhook verify hook for custom authentication** (`register_webhook_verify_hook`)
  - New `@register_webhook_verify_hook` decorator in public API (`apflow.register_webhook_verify_hook`) for tenant-level custom webhook verification
  - Hook receives `WebhookVerifyContext` (task_id, signature, timestamp, client_ip) and returns `WebhookVerifyResult` (valid, user_id, error)
  - Supports both sync and async hook functions
  - Added `WebhookVerifyContext`, `WebhookVerifyResult` dataclasses, and `WebhookVerifyHook` type alias in `core/types.py`

- **`apflow scheduler list` CLI command**
  - List scheduled tasks with filters: `--enabled-only/--all`, `--user-id`, `--type`, `--status`, `--format`
  - Supports both API and direct DB modes via the standard API gateway helper
  - Example: `apflow scheduler list --status running`, `apflow scheduler list --type daily -f json`

- **`apflow scheduler start --verbose` option**
  - New `--verbose` / `-v` flag enables DEBUG-level logging for the scheduler process
  - Works in both foreground and background modes

- **`tasks.scheduled.export-ical` API endpoint**
  - Export scheduled tasks as iCalendar format via JSON-RPC
  - Supports `user_id`, `schedule_type`, `enabled_only`, `limit`, `calendar_name`, and `base_url` parameters
  - Non-admin users can only export their own tasks; admin users can export all

- **Status filter for scheduled task queries**
  - `tasks.scheduled.list` API endpoint and CLI command (`apflow tasks scheduled list`, `apflow scheduler list`) now accept `--status` / `status` parameter to filter by task status (e.g. `pending`, `running`, `completed`, `failed`)
  - `TaskRepository.get_scheduled_tasks()` updated with new `status` parameter

- **`reset_task_tree_for_reexecution()` repository method**
  - Resets all child tasks in a tree to clean pending state (status, result, error, timestamps, progress) before scheduled re-execution
  - Called automatically by `complete_scheduled_run()` for parent tasks with children

- Comprehensive test coverage for admin webhook permissions (`TestWebhookTriggerAdminPermission`: 3 tests)
- Comprehensive test coverage for iCal export API endpoint (`TestScheduledTasksExportIcal`: 4 tests)
- Additional scheduler tests for auto-generated auth token and auth identity logging

### Changed

- **Webhook trigger authentication redesigned — three-layer priority chain**
  - Authentication priority: (1) JWT via middleware → (2) `webhook_verify_hook` for tenant-level custom verification → (3) `APFLOW_WEBHOOK_SECRET` for internal HMAC
  - IP whitelist and rate limit are now applied as an additional protection layer after authentication, instead of being bundled with signature validation
  - Replaces the previous single-`WebhookGateway` approach where all validation was handled in one pass

- **Unified task tree execution model for scheduler**
  - Scheduler (`internal.py`) and webhook gateway (`webhook.py`) now always load the task tree from DB via `get_task_tree_for_api()`, removing the `has_children`-based auto-detection of single vs tree mode
  - Dependency cascade is handled uniformly by `execute_after_task` regardless of task structure

- **Scheduled tasks table now shows Status column**
  - `_print_scheduled_tasks_table()` includes task status with color-coded display (yellow=pending, blue=running, green=completed, red=failed)

- **Refined log levels across scheduler and API routes**
  - Scheduler (`internal.py`): Demoted per-poll-cycle and per-task-execution detail logs from INFO to DEBUG (`Found N due tasks`, `Executing scheduled task`, `Loaded task tree`, `Task not completed (status=pending)`, etc.)
  - Scheduler: Task failures without an explicit error (e.g., `status=pending`) are now logged at DEBUG instead of WARNING
  - API routes (`tasks.py`): Demoted per-request method/params/duration logs from INFO to DEBUG, reducing noise under default INFO level
  - API routes: Demoted CRUD operation detail logs from INFO to DEBUG (`Updated task`, `Creating task tree from N tasks`, `Generated task copy preview`)
  - Removed emoji characters from API route log messages

- **Code formatting applied across codebase** via Black formatter

## [0.14.0] 2026-02-07

### Changed

- **Executor Schema Mechanism — Pydantic BaseModel with ClassVar**
  - All built-in executors now declare input/output schemas as Pydantic `BaseModel` classes assigned via `inputs_schema: ClassVar[type[BaseModel]]` and `outputs_schema: ClassVar[type[BaseModel]]`, replacing inline `get_input_schema()` / `get_output_schema()` method overrides
  - `BaseTask.get_input_schema()` and `get_output_schema()` are now implemented in the base class: they call `model_json_schema()` on the Pydantic model and resolve `$ref` references inline via `resolve_schema_refs()`
  - Subclasses no longer need to override `get_input_schema()` / `get_output_schema()` — just set the `ClassVar`
  - Both Pydantic `BaseModel` and legacy `dict` schemas are still supported (backward compatible)
  - Added `resolve_schema_refs()` helper in `core/utils/helpers.py` to inline `$defs/$ref` from Pydantic-generated JSON Schema
  - Updated all 16 built-in executors: `AggregateResultsExecutor`, `ApiExecutor`, `BatchCrewaiExecutor`, `CrewaiExecutor`, `DockerExecutor`, `SendEmailExecutor`, `GenerateExecutor`, `GrpcExecutor`, `RestExecutor`, `LLMExecutor`, `McpExecutor`, `ScrapeExecutor`, `SshExecutor`, `CommandExecutor`, `SystemInfoExecutor`, `WebSocketExecutor`
  - Updated documentation across 8 files: `extending.md`, `custom-tasks.md`, `quick-reference.md`, `python.md`, `basic_task.md`, `quick-start.md`, `faq.md`, `best-practices.md`

- **API Architecture Refactoring — Three-Layer Design**
  - Introduced unified capabilities registry (`api/capabilities.py`) as single source of truth for all 15 task operations, replacing separate inline definitions in A2A and MCP modules
  - A2A `POST /` now only handles agent-level actions (`tasks.execute`, `tasks.generate`, `tasks.cancel`); CRUD operations return an error directing clients to `POST /tasks`
  - MCP adapter auto-generates all 15 tools from the capabilities registry via `call_by_tool_name` routing
  - Simplified A2A adapter: removed ~300 lines of CRUD bridging code from `agent_executor.py` and `task_routes_adapter.py`

### Added

- **Email Executor** (`send_email_executor`)
  - Send emails via Resend HTTP API or SMTP protocol
  - Provider dispatch: `inputs["provider"]` selects `"resend"` or `"smtp"`
  - Resend provider: POST to `https://api.resend.com/emails` via `httpx.AsyncClient`, returns message ID and status code
  - SMTP provider: Uses `aiosmtplib.send()` (optional dependency, guarded import)
  - Supports plain text and HTML emails, cc/bcc recipients, reply-to address
  - Input validation for required fields (provider, to, subject, from_email, body/html)
  - Schema methods (`get_input_schema`, `get_output_schema`) and demo mode support
  - Added `[email]` optional dependency with `aiosmtplib>=3.0.0`
  - Added `email` to `[all]` installation extra
  - Comprehensive test coverage: 26 tests for validation, Resend, SMTP, schemas, and demo results

- **Method Discovery Endpoint** (`GET /tasks/methods`)
  - Returns all available task methods grouped by category (`agent_action`, `crud`, `query`, `monitoring`) with input schemas, descriptions, and examples
  - Driven by the capabilities registry for automatic consistency

- **Task Scheduling System**
  - Added `ScheduleType` enum supporting six schedule types: `once`, `interval`, `cron`, `daily`, `weekly`, `monthly`
  - Added 9 scheduling fields to `TaskModel`: `schedule_type`, `schedule_expression`, `schedule_enabled`, `schedule_start_at`, `schedule_end_at`, `next_run_at`, `last_run_at`, `max_runs`, `run_count`
  - Created `ScheduleCalculator` module for next execution time calculation across all schedule types
  - Database migration `002_add_scheduling_fields.py` for adding scheduling columns and indexes
  - Automatic execution mode detection: tasks with children execute in tree mode, tasks without children execute in single mode

- **Internal Scheduler**
  - Built-in asyncio-based scheduler (`InternalScheduler`) for standalone deployment
  - Configurable poll interval, max concurrent tasks, task timeout, and user filtering
  - Scheduler lifecycle management: start, stop, pause, resume
  - Task completion callbacks for monitoring and integration

- **External Scheduler Gateway**
  - `WebhookGateway` for HTTP webhook integration with external schedulers
  - HMAC signature validation, IP filtering, and rate limiting
  - Helper functions for generating cron entries and Kubernetes CronJob manifests
  - Support for cron, Kubernetes CronJob, APScheduler, Temporal integration

- **Calendar Integration**
  - `ICalExporter` for exporting scheduled tasks in iCalendar format
  - RRULE support for recurring events (daily, weekly, monthly, interval)
  - Calendar feed URL generation for subscription in Google Calendar, Outlook, etc.

- **Scheduler CLI Commands**
  - `apflow scheduler start` - Start internal scheduler (foreground or background)
  - `apflow scheduler stop` - Stop running scheduler
  - `apflow scheduler status` - Show scheduler status
  - `apflow scheduler trigger` - Manually trigger a task
  - `apflow scheduler export-ical` - Export tasks to iCal format
  - `apflow scheduler webhook-url` - Generate webhook URL for external schedulers

- **Scheduling API Endpoints**
  - `tasks.scheduled.list` - List all scheduled tasks
  - `tasks.scheduled.due` - Get tasks due for execution
  - `tasks.scheduled.init` - Initialize/recalculate next_run_at
  - `tasks.scheduled.complete` - Complete scheduled run and calculate next execution
  - `tasks.webhook.trigger` - Trigger task execution via webhook (JSON-RPC)
  - `POST /webhook/trigger/{task_id}` - REST endpoint for external schedulers

- **Repository Methods for Scheduling**
  - `get_due_scheduled_tasks()` - Query tasks ready for execution
  - `get_scheduled_tasks()` - List scheduled tasks with filters
  - `mark_scheduled_task_running()` - Mark task as in-progress
  - `complete_scheduled_run()` - Complete run and update scheduling state
  - `initialize_schedule()` - Calculate initial next_run_at

- Added `scheduling` optional dependency with `croniter>=1.0.0`
- Added scheduling to `standard` and `all` installation extras
- Comprehensive test coverage: 118 tests for scheduling functionality

## [0.13.0] 2026-01-31

### Added

- **GenerateExecutor Enhancements - Advanced Task Tree Generation System**
  - Dual-mode generation system: `single_shot` (fast) and `multi_phase` (high-quality) modes
  - Multi-phase CrewAI-based task generation with 4-phase specialization:
    - Phase 1: Requirement analysis - understand goals and constraints
    - Phase 2: Structure design - design correct task hierarchies with proper parent-child relationships
    - Phase 3: Input generation - fill task inputs matching executor schemas exactly
    - Phase 4: Review and validation - validate and auto-fix common issues
  - Schema formatter module (`schema_formatter.py`) - provides accurate runtime executor schemas instead of outdated static documentation
  - Principles extractor module (`principles_extractor.py`) - extracts framework principles without code examples to guide LLM generation
  - Enhanced validation system addressing three critical issues:
    - Schema compliance: validates all task inputs match executor schema definitions (required fields, types, constraints)
    - Root task patterns: enforces correct task hierarchy (multi-executor trees must have aggregator root)
    - Auto-fix mechanisms: automatically corrects multiple roots, invalid parent IDs, and schema mismatches
  - Comprehensive test coverage: 359 tests for GenerateExecutor features, multi-phase crew, schema formatting, and principles extraction
  - Documentation: detailed development guide (`docs/development/generate-executor-improvements.md`) and enhanced examples

- Integration tests for schema-based task tree execution and generation, covering scrape_executor + llm_executor, scrape_executor + crewai_executor, and generate_executor flows (tests/integration/test_schema_based_execution.py).
- Improved demo mode test coverage, including custom demo results, sleep scaling, and fallback logic (tests/core/test_demo_mode.py).
- Enhanced TaskBuilder tests for input/output schema handling and multi-level dependencies (tests/core/test_task_builder.py).
- More comprehensive failure handling tests for TaskManager, including all error types and re-execution (tests/core/execution/test_task_failure_handling.py).
- Tests for hook context modification and persistence in pre_hook and post_hook scenarios (tests/core/storage/test_hook_modify_task_with_context.py).
- Integration tests for TaskExecutor with custom and built-in tools (tests/core/execution/test_task_executor_tools_integration.py).
- New `ScrapeExecutor` (`scrape_executor`) for web content extraction and data scraping capabilities
- Extension scanner module (`src/apflow/core/extensions/scanner.py`) for AST-based scanning of extension files
- Test coverage for extension scanner functionality (tests/core/test_extension_scanner.py)
- New `APFLOW_EXTENSIONS_IDS` environment variable for fine-grained control over extension ID-based access restrictions
- Documentation section on task data fields to clarify distinction between inputs, params, and result fields

- **CrewAI Dependency Data Injection Improvements**
  - `CrewaiExecutor` now injects dependency outputs directly into CrewAI task templates and input variables, preventing missing-template-variable errors in complex workflows
  - TaskManager executor instantiation path has been extended to pass structured dependency data into CrewAI-based executors for more reliable multi-phase generation

### Changed

- **GenerateExecutor Improvements**
  - Refactored task tree generation to use runtime executor schemas instead of static documentation, ensuring schema accuracy
  - Moved from single-shot generation to intelligent prompt building with principle extraction and schema formatting
  - Enhanced post-processing to ensure correct user_id handling and UUID validation
  - Improved error handling and validation with detailed logging for debugging
  - Added `generation_mode` parameter to executor inputs for selecting between fast (single_shot) and high-quality (multi_phase) generation

- Refactored WebSocketExecutor to provide full input/output schema, error handling, and cancellation support (src/apflow/extensions/websocket/websocket_executor.py).
- Refactored and cleaned up JSON-RPC API tests for clarity, conciseness, and modern style (tests/api/a2a/test_http_json_rpc.py).
- Improved dummy executor and related test coverage for TaskBuilder (tests/core/test_task_builder.py).
- Enhanced FailingExecutor and related tests for more robust error simulation (tests/core/execution/test_task_failure_handling.py).
- Improved hook context usage and persistence in hook modification tests (tests/core/storage/test_hook_modify_task_with_context.py).
- Expanded demo mode tests to cover more edge cases and sleep scaling (tests/core/test_demo_mode.py).
- Improved schema-based execution tests for dependency mapping and fallback logic (tests/integration/test_schema_based_execution.py).
- Enhanced extension management system with AST-based scanning and detailed configuration options
- Improved extension loading control with executor ID-based restrictions for enhanced security
- Enhanced documentation for CLI command extensions, overrides, and library usage patterns

- Improved CrewAI-based generation dependency handling in TaskManager and `CrewaiExecutor`, ensuring dependency data is consistently available for CrewAI tasks and multi-phase generation flows
- Enhanced `GenerateExecutor` and `SchemaFormatter` with executor exclusion support and lazy loading of multi-phase CrewAI components, reducing overhead when advanced generation features are not used

### Fixed

- Fixed LLM-generated task trees not respecting executor schemas - now validates all inputs match schema definitions
- Fixed poor task tree structure issues - ensures correct root task patterns for multi-executor scenarios
- Fixed outdated documentation in prompts - now uses runtime schemas instead of static markdown docs
- Fixed single-shot LLM quality inconsistencies - added multi-phase mode with specialized crews for complex requirements
- Task failure handling and demo mode functionality improvements with enhanced error tracking and recovery mechanisms
- Improved `ExtensionScanner` path handling for nested project layouts and ensured multi-phase CrewAI tests are skipped cleanly when CrewAI is not installed, including corrected import paths for `MultiPhaseGenerationCrew`

## [0.12.1] 2026-01-25

### Added
- Implemented dependency validation and resolution utilities with corresponding unit tests
- Implemented user ownership validation for task linking and copying
- Added assertions for task_tree_id in TaskCreator tests
- Add project URLs for homepage and source in pyproject.toml

### Changed
- Standardized parameter names in dependency validation functions and enhanced tests for task inclusion validation
- Updated circular dependency detection function signature and tests for consistency
- Refactored dependency validation and resolution logic for clarity and maintainability
- Remove unused imports from test files
- Update directory structure documentation for clarity and organization
- Simplified 'apflow serve' command usage in documentation

### Fixed
- Improved dependency cycle and edge case handling in task linking/copying


## [0.12.0] 2026-01-18

### Added

- **Task Model Enhancements**
  - Added `task_tree_id` field to TaskModel for managing task hierarchy and relationships
  - Introduced `origin_type` field to track task origin (own, link, copy, reference, archive)
  - Implemented `SchemaMigration` model to track migration history for schema evolution
  - Added `has_references` flag validation in task creation tests
  
- **Executor Access Control System**
  - Implemented executor access control via `APFLOW_EXTENSIONS` environment variable
  - Added functions to retrieve allowed executor IDs and filter available executors
  - Created new API endpoint (`/system/executors`) to list available executors with restriction support
  - Introduced CLI commands to list executors with various output formats (table, json, yaml)
  - Added permission checks in TaskManager to enforce executor access control during task execution
  - Comprehensive test coverage for executor permissions and API functionality

- **CLI Extension System Enhancements**
  - **Automatic Root Command Detection**: All single functions registered via `@cli_register` are now automatically treated as root commands (e.g., `apflow version`, `apflow server --port 8000`)
    - Removed `root_command` parameter - no longer needed
    - Functions are always root commands, classes are always groups
    - Simplified API: `@cli_register(name="hello")` creates a root command automatically
  - **Group Extension Support**: Added `group` parameter to `@cli_register` decorator for extending existing CLI groups
    - Extend custom groups: `@cli_register(group="my-group", name="new-command")`
    - Extend built-in groups: `@cli_register(group="tasks", name="custom-action")`
    - Override subcommands: `@cli_register(group="my-group", name="existing", override=True)`
  - **New `get_cli_group()` Function**: Convenient API for accessing and extending CLI groups
    - Supports both registered extensions and built-in groups (tasks, config, etc.)
    - Usage: `group = get_cli_group("my-group"); @group.command() def new_cmd(): ...`
    - Clear error messages when group doesn't exist
  - **Improved Override Behavior**: Clear distinction between different override scenarios
    - `name="my-group", override=True` - Override entire group
    - `group="my-group", name="cmd", override=True` - Override subcommand in group
    - `name="my-cmd", override=True` - Override root command
  - Comprehensive test coverage (8 tests) for all extension scenarios

- **CLI Documentation**
  - Added comprehensive CLI documentation with configuration management guide (`cli/configuration.md`)
  - Created practical usage examples (`cli/examples.md`)
  - Developed detailed CLI usage guide covering installation, modes, and best practices (`cli.md`)
  - Introduced CLI documentation index for improved navigation

- **TaskCreator hanldeing different origin types**
  - Added comprehensive tests for `TaskCreator` methods handling different origin types:
   `from_link`, `from_copy`, `from_archive`, and `from_mixed` methods.
  - Removed create_task_copy* moethods
  - Includes test cases for single tasks, recursive trees, field overrides, dependency handling, and edge cases (error conditions, immutability).


### Changed
- **TaskCreator Logic Refactoring**
  - Refactored and clarified the logic for `from_link`, `from_copy`, `from_archive`, and `from_mixed` methods in `TaskCreator`.
  - Improved type annotations, docstrings, and error handling for all task creation methods.
  - Enhanced dependency and subtree validation logic for recursive copy/link/archive operations.
  - Improved handling of parent/child relationships and task tree construction.
  - Updated internal helper methods for better maintainability and testability.

- **Extension Management Refactoring**
  - Moved executor-related functions from `apflow.api.extensions` to `apflow.core.extensions.manager` for better code organization
  - Updated import paths across the entire codebase to reflect new structure
  - Enhanced TaskManager to load executors dynamically with permission checks
  - Improved on-demand extension loading for better performance and security

- **Decorator Override Logic**
  - All `decorators.py` `override` logic has been changed to `force` logic for consistency and clarity across the codebase.

- **Task Execution Improvements**
  - Enhanced task execution logic with improved error handling
  - Implemented priority grouping for better task scheduling
  - Optimized task tree retrieval using `task_tree_id` with fallback mechanism

- **TaskRepository Refactored**
  - Refactored task update methods in `TaskRepository` for improved maintainability and clarity.
  - Use the method `task_update(self, task_id: str, **kwargs)` instead of all `update_task_*` series functions.
  - Refactored `TaskModel` to `TaskModelType` and enhanced task creation methods for better type safety and extensibility.


### Fixed
- **Database Schema Management**
  - Simplified TaskRepository initialization by removing custom column checks
  - Added migration tests to ensure proper schema evolution and idempotent execution
  - Improved database schema handling for more reliable operations
  - Fixed edge cases in recursive copy/link/archive logic where tasks with external dependencies or disconnected subtrees could cause errors.
  - Improved error messages for invalid task trees and dependency cycles.


## [0.11.2] 2026-01-09

### Added
- Fluent API (TaskBuilder) for streamlined task creation and execution documents

### Changed
- Removed tasks command aliases from CLI documentation and implementation


## [0.11.1] 2026-01-08

### Added
- Distributed core enablement for multi-node orchestration (experimental)
- CLI: implement `task history` command and add short aliases for all major tasks subcommands:
  - `list` → `ls`, `status` → `st`, `cancel` → `c`, `watch` → `w`, `history` → `hi`
  - All aliases are documented and tested
- gRPC executor: support multiple backends and improved channel management

### Changed
- Updated gRPC dependencies and test references from grpcio to grpclib


## [0.11.0] 2026-01-06

### Added
- **Standard Installation Profile** - New `[standard]` extra providing recommended feature set
  - Combines A2A server, CLI tools, CrewAI, and LLM support in single installation command
  - Installation: `pip install apflow[standard]` or `pip install -e ".[standard,dev]"` for development
  - Simplifies setup for most common use cases without requiring manual feature selection
- **Test Infrastructure Improvements**
  - Fixed event loop conflicts in pytest-asyncio test environment via ThreadPoolExecutor-based async isolation
  - New test fixtures: `disable_api_for_tests` for API gateway testing, `run_async` helper for sync test context
  - All 59 CLI tests now passing without hangs (fixed exit code 130 issues)
  - A2A tests excluded by default (added to pytest.ini ignore list since a2a is optional dependency)
  - Full test suite stable: 861 tests passing
- **Security Enhancement: Token Masking**
  - Improved token masking in CLI config display from 8-character preview to 3-character preview
  - Prevents sensitive words from leaking in masked token display
  - Example: "very-secret-token-12345" now masked as "ver...***" instead of "very-sec...***"

### Changed
- **Documentation Updates**
  - Updated README.md to highlight `[standard]` as recommended installation option
  - Updated docs/development/setup.md to recommend `[standard,dev]` for development environment
  - Enhanced installation documentation to explain standard profile benefits
  - Improved test documentation with note about A2A tests being optional
  - Added detailed [standard] configuration documentation in setup.md
- **API Client Enhancement**
  - APIClient.list_tasks() now supports `offset` parameter for pagination beyond initial limit

### Fixed
- **Event Loop Conflict Resolution**
  - Fixed hangs in async CLI tests by implementing ThreadPoolExecutor-based event loop isolation in conftest.py
  - Root cause: run_async_safe() was attempting nested event loop execution in pytest-asyncio context
  - Solution: Patched run_async_safe() to use separate thread with independent event loop
  - Resolves issue where @pytest.mark.asyncio + runner.invoke() caused tests to hang with exit code 130

### TaskBuilder dependency coverage**
  - Added tests for TaskBuilder handling multiple dependencies and multi-level dependency chains to prevent regressions when wiring dependent tasks.
- **ConfigManager integration validation**
  - New integration test verifies `.env` loading and dynamic hook registration work end-to-end via `distribute_task_tree`.
- **CLI → API Gateway Architecture (Priority 2)**
  - New `APIClient` class (`src/apflow/cli/api_client.py`) for CLI to communicate with API server via HTTP
  - ConfigManager extended with API configuration: `api_server_url`, `api_auth_token`, `use_local_db`, `api_timeout`, `api_retry_attempts`, `api_retry_backoff`
  - APIClient supports exponential backoff retry (configurable attempts and initial backoff)
  - APIClient supports auth token injection via Bearer header
  - Comprehensive error handling for connection failures, timeouts, and API errors with custom exception types
  - Added `httpx>=0.27.0` to CLI optional dependencies for HTTP communication
  - Integration tests for APIClient initialization, context manager, and ConfigManager API methods (8 tests)
  - Enables single source of truth (API) for task data when both CLI and API are running
  - Solves DuckDB concurrent write limitation (all writes go through API)
  - Foundation for future protocol adapters (GraphQL, MQTT, WebSocket)
- **CLI Command Layer Refactoring for API Gateway Integration**
  - Created `api_gateway_helper.py` with helper functions for transparent API usage and fallback to local database
  - `should_use_api()`: Check if API is configured
  - `get_api_client_if_configured()`: Async context manager yielding APIClient when configured, None otherwise
  - `api_with_fallback_decorator()`: Try API first, fall back to local database if unavailable
  - `log_api_usage()`: Log whether command is using API or local database
  - Refactored CLI task commands to use API gateway when configured:
    - `tasks list`: Supports API via `client.list_tasks()` with status and user filters
    - `tasks get`: Supports API via `client.get_task()`
    - `tasks status`: Supports API via `client.get_task_status()`
    - `tasks cancel`: Supports API via `client.cancel_task()`
  - All commands automatically fall back to local database if API is not configured
  - Graceful error handling with warning logs when API unavailable but fallback enabled
  - Added property accessors to ConfigManager for convenience: `api_server_url`, `api_auth_token`, `use_local_db`, `api_timeout`, `api_retry_attempts`, `api_retry_backoff`
  - Integration tests for CLI API gateway with fallback scenarios (13 tests)
  - All existing CLI tests pass without modification (59 tests, backward compatible)
- **CLI Configuration Management System with Multi-Location & Separated Secrets**
  - New `apflow config` command with 13 subcommands for comprehensive configuration management
  - **Basic Config**: `set`, `get`, `unset`, `list`, `reset` - Full CRUD operations with alias support
  - **Token Management**: `gen-token`, `verify-token` - Generate and verify JWT tokens with role-based claims
  - **Quick Setup**: `init`, `init-server` - Interactive wizard and one-command server setup
  - **Utilities**: `path`, `show-path`, `edit`, `validate` - Config file management and validation
  - **Multi-location configuration support**:
    - Priority 1 (highest): `APFLOW_CONFIG_DIR` environment variable
    - Priority 2: Project-local `.data/` directory (if in project with pyproject.toml/.git)
    - Priority 3: User-global `~/.aiperceivable/apflow/` (default fallback)
  - **Separated secrets architecture**:
    - `config.cli.yaml` (600 permissions) - Unified configuration (all settings: api_server_url, timeouts, tokens, jwt_secret, etc.)
  - Single file with multi-location priority system (project-local and user-global)
  - API server can also read from same multi-location config structure
  - `gen-token --save` parameter to save tokens to config.cli.yaml
  - Token security: Automatic masking in all outputs, expiry validation, signature verification
  - Alias support for convenience: `api-server`/`api-url` → `api_server_url`, `api-token`/`token` → `admin_auth_token`
  - `apflow config validate` checks YAML syntax, API server connectivity, and token validity
  - `apflow config verify-token` displays token details (subject, issuer, expiry, role) with status indicators
  - `apflow config init` provides interactive wizard for first-time setup
  - Added `PyJWT>=2.8.0` to CLI dependencies for JWT token support
  - 19 new tests for config persistence and CLI commands (all passing)


## [0.10.0] 2026-01-01

### Changed
- **Refactor import paths from aiperceivableflow to apflow across test files**
  - Updated import statements in various test files to reflect the new module structure under apflow.
  - Ensured all references to aiperceivableflow are replaced with apflow in test modules related to extensions, including but not limited to:
    - crewai
    - docker
    - generate
    - grpc
    - http
    - llm
    - mcp
    - ssh
    - stdio
    - websocket
  - Adjusted integration tests to align with the new import paths.


## [0.9.0] 2025-12-28

### Added
- **Hook Execution Context for Database Access**
  - New `get_hook_repository()` function allows hooks to access database within task execution context
  - New `get_hook_session()` function provides direct access to database session in hooks
  - Hooks now share the same database session/transaction as TaskManager (no need for separate sessions)
  - Auto-persistence for `task.inputs` modifications in pre-hooks (detected and saved automatically)
  - Explicit repository methods available for other field modifications (name, priority, status, etc.)
  - Thread-safe context isolation using Python's `ContextVar` (similar to Flask/Celery patterns)
  - Added `set_hook_context()` and `clear_hook_context()` internal functions for context management
  - Exported to public API: `apflow.get_hook_repository` and `apflow.get_hook_session`
  - Added comprehensive test coverage (16 tests):
    - Hook context basic operations and lifecycle
    - Multiple hooks sharing same session instance
    - Hooks sharing transaction context and seeing uncommitted changes
    - Hooks cooperating via shared session
    - Auto-persistence of task.inputs modifications
    - Explicit field updates via repository methods

- **Database Session Safety Enhancements**
  - Added `flag_modified()` calls for all JSON fields (result, inputs, dependencies, params, schemas) to ensure SQLAlchemy detects in-place modifications
  - Added `db.refresh()` after critical status updates to ensure fresh data from database
  - Added concurrent execution protection: same task_tree cannot run multiple times simultaneously
  - Returns `{"status": "already_running"}` when attempting concurrent execution of same task tree
  - Added 12 new tests for concurrent protection and JSON field persistence

- **CLI Extension Decorator (`cli_register`)**
  - New `@cli_register()` decorator for registering CLI extensions, similar to `@executor_register()`
  - Decorator supports `name`, `help`, and `override` parameters
  - Auto-derives command name from class name (converts `_` to `-`)
  - New `get_cli_registry()` function to access registered CLI extensions
  - CLI extensions are loaded from decorator registry before entry_points discovery
  - Added comprehensive test coverage (18 tests) for CLI extension decorators

- **Exception Handling Architecture**
  - New exception hierarchy based on FastAPI/production framework best practices
  - `ApflowError` base exception for all framework-specific errors
  - `BusinessError` for expected user/configuration errors (logged without stack traces)
    - `ValidationError` for input validation failures
    - `ConfigurationError` for missing configuration/dependencies
  - `SystemError` for unexpected system-level errors (logged with stack traces)
    - `ExecutorError` for executor runtime failures
    - `StorageError` for database/storage failures
  - Created `core/execution/errors.py` with comprehensive exception documentation
  - Created `docs/development/exception.md` with implementation guidelines

### Changed
- **Executor Error Handling Refactoring**
  - All executors now raise exceptions instead of returning error dictionaries
  - Technical exceptions (TimeoutError, ConnectionError, etc.) now propagate naturally to TaskManager
  - Executors validate inputs and raise `ValidationError` or `ConfigurationError` for expected failures
  - Updated executors: `DockerExecutor`, `GrpcExecutor`, `RestExecutor`, `SshExecutor`, `CommandExecutor`, `LLMExecutor`
  - TaskManager now catches all exceptions, marks tasks as failed, and logs appropriately based on exception type
  - `BusinessError` logged without stack trace (clean logs), other exceptions logged with full stack trace

- **CrewAI Executor/Batch Executor Renaming and Test Fixes**
  - `crewai/crew_manager.py` → `crewai/crewai_executor.py`, with the class name updated to `CrewaiExecutor`
  - `crewai/batch_manager.py` → `crewai/batch_crewai_executor.py`, with the class name updated to `BatchCrewaiExecutor`
  - All related test cases (test_crewai_executor.py, test_batch_crewai_executor.py, etc.) have been batch-updated with corrected patch paths, mocks, imports, and class names to align with the new naming
  - Resolved AttributeError: module 'apflow.extensions.crewai' has no attribute 'crew_manager' and similar test failures caused by the renaming


## [0.8.0] 2025-12-25

### Added
- **LLM Executor Integration**
  - Added `LLMExecutor` (`llm_executor`) for direct LLM interaction via LiteLLM
  - Supports unified `model` parameter for 100+ providers (OpenAI, Anthropic, Gemini, etc.)
  - Support for `stream=True` in inputs or context metadata for Server-Sent Events (SSE)
  - Automatic API key handling via `LLMKeyConfigManager` or environment variables
  - Auto-registration via extensions mechanism
  - Added `[llm]` optional dependency including `litellm`

- **CLI: Plugin Mechanism for Extensions**
  - Added `CLIExtension` class to facilitate creating CLI subcommands in external projects.
  - Implemented dynamic subcommands discovery using Python `entry_points` (`apflow.cli_plugins`).
  - Allows projects like `apflow-demo` to register commands (e.g., `apflow users stat`) without modifying the core library.
  - Supports both full `typer.Typer` apps and single-command callables as plugins.

- **CLI: Improved Task Count Output**
  * Changed default output format of `apflow tasks count` from `json` to `table` for better terminal readability.

### Changed
- **CLI: Simplified `apflow tasks` commands**
  - `apflow tasks count` now defaults to providing comprehensive database statistics grouped by status.
  - Removed redundant `--all` and `--status` flags from `count` command (database statistics are now the default).
  - Renamed `apflow tasks all` command to `apflow tasks list` for better alignment with API naming conventions.
  - Removed the legacy `apflow tasks list` command (which only showed running tasks).
  - The new `apflow tasks list` command now lists all tasks from the database with support for filtering and pagination.

### Fixed
- **Tests: Infrastructure and LLM Integration**
  - Updated `tests/conftest.py` to automatically load `.env` file environment variables at the start of the test session.
  - Added auto-registration for `LLMExecutor` in the test `conftest.py` fixture.
  - Fixed `LLMExecutor` integration tests to correctly use real API keys from `.env` when available.

## [0.7.3] 2025-12-22

### Fixed
- **CLI: event-loop handling for async database operations**
  - Ensured async database sessions and repositories are created and closed inside
    the same event loop to avoid "no running event loop" and "Event loop is closed" errors
  - Updated `apflow tasks` commands to run async work in a safe context
  - Added `nest_asyncio` support for nested event loops in test environments

- **Logging: clean CLI output by default**
  - Default log level for the library is now `ERROR` to keep CLI output clean
  - Support `LOG_LEVEL` and `DEBUG` environment variables to override logging when needed
  - Debug logs can be enabled with `LOG_LEVEL=DEBUG apflow ...`

- **Extensions registration noise reduced**
  - Demoted expected registration instantiation messages to `DEBUG` (no longer printed by default)
  - This prevents benign initialization messages from appearing during normal CLI runs

- **Miscellaneous**
  - Added `nest_asyncio` to CLI optional dependencies to improve compatibility in nested-loop contexts

## [0.7.2] 2025-12-21

### Fixed
- **Documentation Corrections for `schemas.method` Field**
  - Clarified that `schemas.method` is a required field when `schemas` is provided
  - Updated documentation to explicitly state that `schemas.method` must match an executor ID from the extensions registry
  - Fixed all documentation examples to use real executor IDs instead of placeholder values
  - Updated examples across all documentation files:
    - `docs/api/http.md`: Replaced generic `"executor_id"` with concrete IDs like `"system_info_executor"`, `"rest_executor"`, `"command_executor"`
    - `docs/getting-started/quick-start.md`: Updated all task examples to use valid executor IDs
    - `docs/guides/cli.md`: Fixed CLI command examples with correct executor IDs
    - `docs/development/design/cli-design.md`: Updated design documentation examples
    - `docs/development/setup.md`: Fixed setup guide examples
  - Fixed `generate_executor.py` LLM prompt to correctly instruct LLM to use `schemas.method` (not `name`) as executor ID
  - Updated task structure examples in LLM prompt to reflect correct usage

- **API Endpoint Test Coverage**
  - Added missing test cases for API endpoints:
    - `test_jsonrpc_tasks_list`: Tests `tasks.list` endpoint with pagination
    - `test_jsonrpc_tasks_running_status`: Tests `tasks.running.status` endpoint with array format
    - `test_jsonrpc_tasks_running_count`: Tests `tasks.running.count` endpoint
    - `test_jsonrpc_tasks_cancel`: Tests `tasks.cancel` endpoint with array format
    - `test_jsonrpc_tasks_generate`: Tests `tasks.generate` endpoint for task tree generation
  - Fixed test parameter format issues:
    - `tasks.running.status` and `tasks.cancel` now correctly use `task_ids` array parameter instead of single `task_id`
    - Tests now expect array responses instead of single object responses
  - All API endpoints now have comprehensive test coverage

- **CLI Command Test Coverage**
  - Added `test_tasks_watch` test cases for `tasks watch` CLI command
    - Uses mock to avoid interactive `Live` display component issues in automated tests
    - Tests parameter validation and basic functionality
    - Properly handles error messages in stderr

- **API Documentation Completeness**
  - Added missing response example for `tasks.running.status` endpoint
    - Includes complete response format with all fields (task_id, context_id, status, progress, error, is_running, timestamps)
    - Documents error cases (not_found, permission_denied)
    - Clarifies that method returns array format even for single task queries

### Added
- **Comprehensive Documentation Review**
  - Verified all documentation examples use valid executor IDs
  - Ensured all examples are functional and can be parsed correctly
  - Validated that all CLI commands have corresponding test cases
  - Confirmed API endpoint documentation matches actual implementation

## [0.7.1] 2025-12-20

### Fixed
- **DuckDB Custom Path Directory Creation**
  - Fixed issue where DuckDB would fail when using custom directory paths that don't exist
  - Added `_ensure_database_directory_exists()` function to automatically create parent directories before creating DuckDB connections
  - Directory creation is now handled automatically in `create_session()`, `SessionPoolManager.initialize()`, and `PooledSessionContext.__init__()`
  - Skips directory creation for in-memory databases (`:memory:`) and handles errors gracefully with appropriate logging
  - Users can now specify custom DuckDB file paths without manually creating directories first
- **Missing Return Type Annotations**
  - Added missing return type annotation `-> None` to `check_input_schema()` function in `core/utils/helpers.py`
  - Added missing return type annotation `-> ParseResult` to `validate_url()` function in `core/utils/helpers.py`
  - Fixed type checker errors and ensured 100% type annotation compliance as required by code quality rules
- **Module-Level Resource Creation**
  - Refactored `core/storage/factory.py` to eliminate module-level global variables for database sessions
  - Replaced `_default_session` and `_session_pool_manager` module-level globals with `SessionRegistry` class
  - Session state is now encapsulated in `SessionRegistry` class following dependency injection principles
  - All session management functions (`get_default_session()`, `set_default_session()`, `reset_default_session()`, `get_session_pool_manager()`, `reset_session_pool_manager()`) now use `SessionRegistry` class methods
  - Maintains full backward compatibility - all public APIs remain unchanged
  - Follows code quality rules requiring dependency injection instead of module-level resource creation


## [0.7.0] 2025-12-20

### Added
- **Task Context Sharing and LLM Key Management**
  - **Task Context Sharing**: TaskManager now passes the entire `task` object (TaskModel instance) to executors
    - Executors can access all task fields including custom TaskModel fields via `self.task`
    - Supports custom TaskModel classes with additional fields
    - Enables executors to modify task context (e.g., update status, progress, custom fields)
    - BaseTask uses weak references (`weakref.ref`) to store task objects, preventing memory leaks
    - Task context is automatically cleared after execution or cancellation
    - `task_id` is stored separately for future extension (e.g., Redis-based task storage)
  - **Unified user_id Access**: BaseTask provides `user_id` property that automatically retrieves from `task.user_id`
    - Executors can use `self.user_id` instead of `inputs.get("user_id")`
    - Falls back to `_user_id` when task is not available (for backward compatibility and testing)
    - All LLM executors (`generate_executor`, `crewai_executor`) now use `self.user_id`
  - **Unified LLM Key Retrieval**: Centralized LLM key management with context-aware priority order
    - New `get_llm_key()` function with unified priority logic for API and CLI contexts
    - API context priority: header → LLMKeyConfigManager → environment variables
    - CLI context priority: params → LLMKeyConfigManager → environment variables
    - Auto-detection mode (`context="auto"`) automatically detects API or CLI context
    - All LLM executors now proactively retrieve keys using unified mechanism
    - Removed hardcoded LLM key injection logic from TaskManager (separation of concerns)
  - **LLM Key Context Optimization**: Refactored `llm_key_context.py` to eliminate code duplication
    - Extracted `_get_key_from_user_config()` helper function for user config lookup
    - Extracted `_get_key_from_source()` helper function for header/CLI params retrieval
    - Reduced code duplication by ~40%, improved maintainability
    - All functionality preserved, backward compatible

- **Enhanced Task Copy Functionality**
  - **UUID Generation for Task IDs**: Task copy now always generates new UUIDs for copied tasks, regardless of `save` parameter value
    - Ensures clear task tree relationships and prevents ID conflicts
    - All copied tasks receive unique IDs for proper dependency mapping
    - Compatible with `tasks.create` API when `save=False` (returns task array with complete data)
  - **Save Parameter Support**: New `save` parameter for `create_task_copy()` method and `tasks.copy` API
    - `save=True` (default): Saves copied tasks to database and returns TaskTreeNode
    - `save=False`: Returns task array without saving to database, suitable for preview or direct use with `tasks.create`
    - Task array format includes all required fields (id, name, parent_id, dependencies) with new UUIDs
    - Dependencies correctly reference new task IDs within the copied tree
  - **Parameter Renaming for Clarity**: Renamed parameters in custom copy mode for better clarity
    - `task_ids` → `custom_task_ids` (required when `copy_mode="custom"`)
    - `include_children` → `custom_include_children` (used when `copy_mode="custom"`)
    - Old parameter names removed (no backward compatibility)
    - CLI: `--task-ids` → `--custom-task-ids`, `--include-children` → `--custom-include-children`
  - **Improved Dependency Mapping**: Fixed dependency resolution in copied task trees
    - Dependencies now correctly reference new task IDs within the copied tree
    - Original task IDs properly mapped to new IDs for all tasks in the tree
    - Circular dependency detection works correctly with new task IDs
    - `original_task_id` correctly points to each task's direct original counterpart (not root)
  - **Comprehensive Test Coverage**: Added extensive test cases for API and CLI
    - API tests: 11 test cases covering all copy modes, save parameter, error handling
    - CLI tests: 7 test cases covering all copy modes, dry-run, reset_fields
    - Tests verify UUID generation, dependency mapping, and database interaction
- **API Module Refactoring for Better Library Usage**
  - Split `api/main.py` into modular components for improved code organization
  - New `api/extensions.py`: Extension management module with `initialize_extensions()` and extension configuration
  - New `api/protocols.py`: Protocol management module with protocol selection and dependency checking
  - New `api/app.py`: Application creation module with `create_app_by_protocol()` and protocol-specific server creation functions
  - `api/main.py` now contains library-friendly entry points (`main()` and `create_runnable_app()` functions)
  - **Benefits**: Better separation of concerns, easier to use in external projects like apflow-demo
  - **Migration**: Import paths updated:
    - `from apflow.api.extensions import initialize_extensions`
    - `from apflow.api.protocols import get_protocol_from_env, check_protocol_dependency`
    - `from apflow.api.app import create_app_by_protocol, create_a2a_server, create_mcp_server`
  - All existing imports from `api/main` continue to work via re-exports for backward compatibility

- **Enhanced Library Usage Support in `api/main.py`**
  - **New `create_runnable_app()` function**: Replaces `create_app()` with clearer naming
    - Returns a fully initialized, runnable application instance
    - Handles all initialization steps: .env loading, extension initialization, custom TaskModel loading, examples initialization
    - Supports custom routes, middleware, and TaskRoutes class via `**kwargs`
    - Can be used when you need the app object but want to run the server yourself
    - Usage: `from apflow.api.main import create_runnable_app; app = create_runnable_app()`
  - **Enhanced `main()` function**: Now fully supports library usage
    - Can be called directly from external projects with custom configuration
    - Separates application configuration (passed to `create_runnable_app()`) from server configuration (uvicorn parameters)
    - Supports all uvicorn parameters: `host`, `port`, `workers`, `loop`, `limit_concurrency`, `limit_max_requests`, `access_log`
    - Usage: `from apflow.api.main import main; main(custom_routes=[...], port=8080)`
  - **Smart .env File Loading**: New `_load_env_file()` function with priority-based discovery
    - Priority order: 1) Current working directory, 2) Main script's directory, 3) Library's own directory (development only)
    - Ensures that when used as a library, it loads `.env` from the consuming project, not from the library's installation directory
    - Respects existing environment variables (`override=False`)
    - Gracefully handles missing `python-dotenv` package
  - **Development Environment Setup**: New `_setup_development_environment()` function
    - Only runs when executing library's own `main.py` directly (not when installed as package)
    - Suppresses specific warnings for cleaner output
    - Adds project root to Python path for development mode
    - Does not affect library usage in external projects
  - **Backward Compatibility**: All existing code continues to work
    - `create_app()` name deprecated but still available via alias
    - All initialization steps remain the same, just better organized

- **Enhanced API Server Creation Functions**
  - Added `auto_initialize_extensions` parameter to `create_a2a_server()` in `api/a2a/server.py`
  - Matches behavior of `create_app_by_protocol()` for consistent API
  - Default: `False` (backward compatible)
  - Added `task_routes_class` parameter to `create_app_by_protocol()` and server creation functions
  - Supports custom `TaskRoutes` class injection throughout the server creation chain
  - Enables apflow-demo to use standard API functions directly without workarounds
  - All new parameters are optional with safe defaults for backward compatibility

- **Executor Metadata API**
  - New `get_executor_metadata(executor_id)` function to query executor metadata
  - New `validate_task_format(task, executor_id)` function to validate tasks against executor schemas
  - New `get_all_executor_metadata()` function to get metadata for all executors
  - Located in `apflow.core.extensions.executor_metadata`
  - Used by demo applications to generate accurate demo tasks
  - Returns: id, name, description, input_schema, examples, tags

### Removed
- **Examples Module Deprecation**
  - Removed `apflow.examples` module from core library
  - Removed `examples` CLI command (`apflow examples init`)
  - Removed `examples = []` optional dependency from `pyproject.toml`
  - **Migration**: Demo task initialization has been moved to the **apflow-demo** project
  - Demo task definitions are now managed separately from the core library
  - This keeps the core library focused on orchestration functionality
  - For demo tasks, please use [apflow-demo](https://github.com/aiperceivable/apflow-demo)

- **Examples API Methods**
  - Removed `examples.init` and `examples.status` API methods from system routes
  - These methods are no longer available in the API
  - **Migration**: Use apflow-demo for demo task initialization

### Changed
- **Session Management Refactoring**
  - Replaced `get_default_session()` with `create_pooled_session()` context manager in all API routes
  - Renamed `create_task_tree_session` to `create_pooled_session` in `storage/factory.py`
  - Updated `TaskExecutor` to use `create_pooled_session` as fallback
  - Improved concurrency safety for API requests
  - **Breaking Change**: `get_default_session()` is now deprecated for route handlers
- **LLM Key Management Architecture**
  - Executors now proactively retrieve LLM keys instead of receiving them via inputs
  - TaskManager no longer handles LLM key injection for specific executors
  - LLM key retrieval is now executor responsibility, following separation of concerns
  - All executors use unified `get_llm_key()` function with consistent priority order


## [0.6.1] 2025-12-11

### Added
- **JWT Token Generation Support**
  - New `generate_token()` function in `apflow.api.a2a.server` for generating JWT tokens
  - Supports custom payload, secret key, algorithm (default: HS256), and expiration (default: 30 days)
  - Uses `python-jose[cryptography]` for token generation and verification
  - Complements existing `verify_token()` function for complete JWT token lifecycle management
  - Usage: `from apflow.api.a2a.server import generate_token; token = generate_token({"user_id": "user123"}, secret_key)`

- **Cookie-based JWT Authentication**
  - Support for JWT token extraction from `request.cookies.get("Authorization")` in addition to Authorization header
  - Priority: Authorization header is checked first, then falls back to cookie if header is not present
  - Enables cookie-based authentication for web applications and browser-based clients
  - Maintains security: Only JWT tokens are trusted (no fallback to HTTP headers for user identification)
  - Updated `_extract_user_id_from_request()` method in `BaseRouteHandler` to support both header and cookie sources

- **Dependency Updates**
  - Added `python-jose[cryptography]>=3.3.0` to `[a2a]` optional dependencies in `pyproject.toml`
  - Required for JWT token generation and verification functionality


## [0.6.0] 2025-12-10

### Added
- **TaskRoutes Extension Mechanism**
  - Added `task_routes_class` parameter to `create_a2a_server()` and `_create_request_handler()` for custom TaskRoutes injection
  - Eliminates the need for monkey patching when extending TaskRoutes functionality
  - Supports custom routes via `custom_routes` parameter in `CustomA2AStarletteApplication`
  - Backward compatible: optional parameter with default `TaskRoutes` class
  - Usage: `create_a2a_server(task_routes_class=CustomTaskRoutes, custom_routes=[...])`

- **Task Tree Lifecycle Hooks**
  - New `register_task_tree_hook()` decorator for task tree lifecycle events
  - Four hook types: `on_tree_created`, `on_tree_started`, `on_tree_completed`, `on_tree_failed`
  - Explicit lifecycle tracking without manual root task detection
  - Hooks receive root task and relevant context (status, error message)
  - Usage: `@register_task_tree_hook("on_tree_completed") async def on_completed(root_task, status): ...`

- **Executor-Specific Hooks**
  - Added `pre_hook` and `post_hook` parameters to `@executor_register()` decorator
  - Runtime hook registration via `add_executor_hook(executor_id, hook_type, hook_func)`
  - Inject custom logic (e.g., quota checks, demo data fallback) for specific executors
  - `pre_hook` can return a result to skip executor execution (useful for demo mode)
  - `post_hook` receives executor, task, inputs, and result for post-processing
  - Supports both decorator-based and runtime registration for existing executors

- **Automatic user_id Extraction**
  - Automatic `user_id` extraction from JWT token in `TaskRoutes.handle_task_generate` and `handle_task_create`
  - Only extracts from JWT token payload for security (HTTP headers can be spoofed)
  - Supports `user_id` field or standard JWT `sub` claim in token payload
  - Extracted `user_id` automatically set on task data
  - Simplifies custom route implementations and ensures consistent user identification
  - Security: Only trusted JWT tokens are used, no fallback to HTTP headers

- **Demo Mode Support**
  - Built-in demo mode via `use_demo` parameter in task inputs
  - CLI support: `--use-demo` flag for `apflow run flow` command
  - API support: `use_demo` parameter in task creation and execution
  - Executors can override `get_demo_result()` method in `BaseTask` for custom demo data
  - Default demo data format: `{"result": "Demo execution result", "demo_mode": True}`
  - All built-in executors now implement `get_demo_result()` method:
    - `SystemInfoExecutor`, `CommandExecutor`, `AggregateResultsExecutor`
    - `RestExecutor`, `GenerateExecutor`, `ApiExecutor`
    - `SshExecutor`, `GrpcExecutor`, `WebSocketExecutor`
    - `McpExecutor`, `DockerExecutor`
    - `CrewaiExecutor`, `BatchCrewaiExecutor` (CrewAI executors)
  - **Realistic Demo Execution Timing**: All executors include `_demo_sleep` values to simulate real execution time:
    - Network operations (HTTP, SSH, API): 0.2-0.5 seconds
    - Container operations (Docker): 1.0 second
    - LLM operations (CrewAI, Generate): 1.0-1.5 seconds
    - Local operations (SystemInfo, Command, Aggregate): 0.05-0.1 seconds
  - **Global Demo Sleep Scale**: Configurable via `APFLOW_DEMO_SLEEP_SCALE` environment variable (default: 1.0)
    - Allows adjusting demo execution speed globally (e.g., `0.5` for faster, `2.0` for slower)
    - API: `set_demo_sleep_scale(scale)` and `get_demo_sleep_scale()` functions
  - **CrewAI Demo Support**: `CrewaiExecutor` and `BatchCrewaiExecutor` generate realistic demo results:
    - Based on `works` definition (agents and tasks) from task params or schemas
    - Includes simulated `token_usage` matching real LLM execution patterns
    - `BatchCrewaiExecutor` aggregates token usage across multiple works
  - Demo mode helps developers test workflows without external dependencies

- **TaskModel Customization Improvements**
  - Enhanced `set_task_model_class()` with improved validation and error messages
  - New `@task_model_register()` decorator for convenient TaskModel registration
  - Validation ensures custom classes inherit from `TaskModel` with helpful error messages
  - Supports `__table_args__ = {'extend_existing': True}` for extending existing table definitions
  - Better support for user-defined `MyTaskModel(TaskModel)` with additional fields

  - **Documentation for Hook Types**
  - Added comprehensive documentation explaining differences between hook types
  - `pre_hook` / `post_hook`: Task-level hooks for individual task execution
  - `task_tree_hook`: Task tree-level hooks for tree lifecycle events
  - Clear usage scenarios and examples in `docs/development/extending.md`

### Changed
- **LLM Model Parameter Naming**: Unified LLM model parameter naming to `model` across all components
  - **Breaking Change**: `llm_model` parameter in `generate_executor` has been renamed to `model`
    - GenerateExecutor: `inputs["llm_model"]` → `inputs["model"]`
    - API Routes: `params["llm_model"]` → `params["model"]`
    - CLI: `--model` parameter remains unchanged (internal mapping updated)
  - **New Feature**: Support for `schemas["model"]` configuration for CrewAI executor
    - Model configuration can now be specified in task schemas and will be passed to CrewaiExecutor
    - Priority: `schemas["model"]` > `params.works.agents[].llm` (CrewAI standard)
  - **Impact**: Only affects generate functionality introduced in 0.5.0, minimal breaking change
  - **Migration**: Update any code using `llm_model` parameter to use `model` instead

### Removed
- **Redundant decorators.py file**
  - Removed `src/apflow/decorators.py` as it was no longer used
  - Functionality superseded by `src/apflow/core/decorators.py`
  - No impact on existing code (file was not imported by any other modules)


## [0.5.0] 2025-12-7

### Added

- **Extended Executor Framework with Mainstream Execution Methods**
  - **HTTP/REST API Executor** (`rest_executor`)
    - Support for all HTTP methods (GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS)
    - Authentication support (Bearer token, Basic auth, API keys)
    - Configurable timeout and retry mechanisms
    - Request/response headers and body handling
    - JSON and form data support
    - Comprehensive error handling for HTTP status codes
    - Full test coverage with 15+ test cases
  
  - **SSH Remote Executor** (`ssh_executor`)
    - Execute commands on remote servers via SSH
    - Support for password and key-based authentication
    - Key file validation with security checks (permissions, existence)
    - Environment variable injection
    - Custom SSH port configuration
    - Timeout and cancellation support
    - Comprehensive error handling for connection and execution failures
    - Full test coverage with 12+ test cases
  
  - **Docker Container Executor** (`docker_executor`)
    - Execute commands in isolated Docker containers
    - Support for custom Docker images
    - Volume mounts for data persistence
    - Environment variable configuration
    - Resource limits (CPU, memory)
    - Container lifecycle management (create, start, wait, remove)
    - Timeout handling with automatic container cleanup
    - Option to keep containers after execution
    - Comprehensive error handling for image not found, execution failures
    - Full test coverage with 13+ test cases
  
  - **gRPC Executor** (`grpc_executor`)
    - Call gRPC services and microservices
    - Support for dynamic proto file loading
    - Method invocation with parameter serialization
    - Metadata and timeout configuration
    - Error handling for gRPC status codes
    - Support for unary, server streaming, client streaming, and bidirectional streaming
    - Full test coverage with 10+ test cases
  
  - **WebSocket Executor** (`websocket_executor`)
    - Bidirectional WebSocket communication
    - Send and receive messages in real-time
    - Support for JSON and text messages
    - Custom headers for authentication
    - Optional response waiting with timeout
    - Connection error handling (invalid URI, connection closed, timeout)
    - Cancellation support
    - Full test coverage with 13+ test cases
  
  - **apflow API Executor** (`apflow_api_executor`)
    - Call other apflow API instances for distributed execution
    - Support for all task management methods (tasks.execute, tasks.create, tasks.get, etc.)
    - Authentication via JWT tokens
    - Task completion polling with production-grade retry logic:
      - Exponential backoff on failures (1s → 2s → 4s → 8s → 30s max)
      - Circuit breaker pattern (stops after 10 consecutive failures)
      - Error classification (retryable vs non-retryable)
      - Total failure threshold (20 failures across all polls)
      - Detailed logging for debugging
    - Timeout protection and cancellation support
    - Comprehensive error handling for network, server, and client errors
    - Full test coverage with 12+ test cases
  
  - **Dependency Management**
    - Optional dependencies for new executors:
      - `[ssh]`: asyncssh for SSH executor
      - `[docker]`: docker for Docker executor
      - `[grpc]`: grpcio, grpcio-tools for gRPC executor
      - `[all]`: Includes all optional dependencies
    - Graceful handling when optional dependencies are not installed
    - Clear error messages with installation instructions
  
  - **Documentation**
    - Comprehensive usage examples for all new executors in `docs/guides/custom-tasks.md`
    - Configuration parameters and examples for each executor
    - Best practices and common patterns
    - Error handling guidelines
  
  - **Auto-discovery**
    - All new executors automatically registered via extension system
    - Auto-imported in API service startup
    - Available immediately after installation

- **MCP (Model Context Protocol) Executor** (`mcp_executor`)
  - Interact with MCP servers to access external tools and data sources
  - Support for stdio and HTTP transport modes
  - Operations: list_tools, call_tool, list_resources, read_resource
  - JSON-RPC 2.0 protocol compliance
  - Environment variable injection for stdio mode
  - Custom headers support for HTTP mode
  - Timeout and cancellation support
  - Comprehensive error handling for MCP protocol errors
  - Full test coverage with 20+ test cases

- **MCP (Model Context Protocol) Server** (`api/mcp/`)
  - Expose apflow task orchestration capabilities as MCP tools and resources
  - Support for stdio and HTTP/SSE transport modes
  - MCP Tools (8 tools):
    - `execute_task` - Execute tasks or task trees
    - `create_task` - Create new tasks or task trees
    - `get_task` - Get task details by ID
    - `update_task` - Update existing tasks
    - `delete_task` - Delete tasks (if all pending)
    - `list_tasks` - List tasks with filtering
    - `get_task_status` - Get status of running tasks
    - `cancel_task` - Cancel running tasks
  - MCP Resources:
    - `task://{task_id}` - Access individual task data
    - `tasks://` - Access task list with query parameters
  - JSON-RPC 2.0 protocol compliance
  - Integration with existing TaskRoutes for protocol-agnostic design
  - HTTP mode: FastAPI/Starlette integration with `/mcp` endpoint
  - stdio mode: Standalone process for local integration
  - Comprehensive error handling with proper HTTP status codes
  - Full test coverage with 45+ test cases across all components
  - Protocol selection via `APFLOW_API_PROTOCOL=mcp` environment variable
  - CLI protocol selection: `--protocol` parameter for `serve` and `daemon` commands
    - Default protocol: `a2a`
    - Supported protocols: `a2a`, `mcp`
    - Usage: `apflow serve --protocol mcp` or `apflow daemon start --protocol mcp`

- **Task Tree Generator Executor** (`generate_executor`)
  - Generate valid task tree JSON arrays from natural language requirements using LLM
  - Automatically collects available executors and their input schemas for LLM context
  - Loads framework documentation (task orchestration, examples, concepts) as LLM context
  - Supports multiple LLM providers (OpenAI, Anthropic) via configurable backend
  - Comprehensive validation ensures generated tasks conform to TaskCreator requirements:
    - Validates task structure (name, id consistency, parent_id, dependencies)
    - Detects circular dependencies
    - Ensures single root task
    - Validates all references exist in the array
  - LLM prompt engineering with framework context, executor information, and examples
  - JSON response parsing with markdown code block support
  - Can be used through both API and CLI as a standard executor
  - **API Endpoint**: `tasks.generate` method via JSON-RPC `/tasks` endpoint
    - Supports all LLM configuration parameters (provider, model, temperature, max_tokens)
    - Optional `save` parameter to automatically save generated tasks to database
    - Returns generated task tree JSON array with count and status message
    - Full test coverage with 8 API endpoint test cases
  - **CLI Command**: `apflow generate task-tree` for direct task tree generation
    - Supports output to file or stdout
    - Optional database persistence with `--save` flag
    - Comprehensive test command examples in documentation
  - Configuration via environment variables or input parameters:
    - `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` for LLM authentication
    - `APFLOW_LLM_PROVIDER` for provider selection (default: openai)
    - `OPENAI_MODEL` or `ANTHROPIC_MODEL` for model selection
  - Full test coverage with 28+ executor test cases and 8 API endpoint test cases
  - Usage examples:
    ```python
    # Python API
    task = await task_manager.task_repository.create_task(
        name="generate_executor",
        inputs={"requirement": "Fetch data from API, process it, and save to database"}
    )
    ```
    ```json
    // JSON-RPC API
    {
      "jsonrpc": "2.0",
      "method": "tasks.generate",
      "params": {
        "requirement": "Fetch data from API and process it",
        "save": true
      }
    }
    ```
    ```bash
    # CLI
    apflow generate task-tree "Fetch data from API and process it" --save
    ```

## [0.4.0] - 2025-12-5

### Added
- **CLI Task Commands Synchronization with API**
  - Complete synchronization of CLI task commands with API task routes
  - Unified data format: CLI commands now return the same data structure as API endpoints using `task.to_dict()`
  - New CLI commands matching API functionality:
    - `tasks get <task_id>` - Get task details (equivalent to `tasks.get` API)
    - `tasks create --file <file>|--stdin` - Create task tree from JSON file or stdin (equivalent to `tasks.create` API)
    - `tasks update <task_id> [options]` - Update task fields (equivalent to `tasks.update` API)
    - `tasks delete <task_id> [--force]` - Delete task with validation (equivalent to `tasks.delete` API)
    - `tasks tree <task_id>` - Get task tree structure (equivalent to `tasks.tree` API)
    - `tasks children --parent-id <id>|--task-id <id>` - Get child tasks (equivalent to `tasks.children` API)
    - `tasks all [options]` - List all tasks from database with filters (equivalent to `tasks.list` API)
  - Enhanced existing commands:
    - `tasks list` - Now returns full `task.to_dict()` format matching API `tasks.running.list`
    - `tasks status` - Now includes `context_id`, `started_at`, and `updated_at` fields matching API format
  - All CLI commands maintain API compatibility for consistent data formats
  - Comprehensive test coverage with 43 test cases covering all CLI task commands

- **Unified A2A Protocol Task Management**
  - All task management operations now fully supported through A2A Protocol `/` route
  - Standardized method naming: `tasks.execute`, `tasks.create`, `tasks.get`, `tasks.update`, `tasks.delete`, `tasks.detail`, `tasks.tree`, `tasks.children`, `tasks.running.list`, `tasks.running.status`, `tasks.running.count`, `tasks.cancel`, `tasks.copy`, `tasks.generate`
  - `TaskRoutesAdapter` component bridges A2A Protocol `RequestContext`/`EventQueue` with existing `TaskRoutes` handlers
  - Automatic conversion between A2A Protocol format and internal task representation
  - Real-time task status updates via `TaskStatusUpdateEvent` for all task management operations
  - Backward compatibility: `execute_task_tree` skill ID still supported (maps to `tasks.execute`)
  - All 14 task management skills registered in Agent Card for protocol compliance
  - Comprehensive test coverage with 17+ test cases for all task management methods
  - Fixed `MessageSendConfiguration` access error: Properly handle Pydantic model attributes instead of dictionary methods

### Fixed
- **A2A Protocol Configuration Access**
  - Fixed `AttributeError: 'MessageSendConfiguration' object has no attribute 'get'` in `TaskRoutesAdapter`
  - Properly handle Pydantic model attributes using `getattr()` and `model_dump()` instead of dictionary methods
  - Compatible with both Pydantic v1 and v2
  - All 5 previously failing integration tests now pass

- **A2A Protocol Cancel Method Implementation**
  - Complete implementation of `AgentExecutor.cancel()` method for A2A Protocol
  - Task ID extraction with priority: `task_id` > `context_id` > `metadata.task_id` > `metadata.context_id`
  - Support for custom error messages via `metadata.error_message`
  - Graceful cancellation by calling executor's `cancel()` method if supported
  - Token usage and partial results preservation during cancellation
  - Proper `TaskStatusUpdateEvent` generation with A2A Protocol compliance
  - Comprehensive error handling for task not found, already completed, and exception scenarios
  - Complete test coverage with 13 test cases covering all scenarios
  - Documentation updates in HTTP API and Python API references

- **Enhanced Task Copy Functionality**
  - `children` parameter for `create_task_copy()`: When `True`, also copy each direct child task with its dependencies
  - Deduplication ensures tasks depending on multiple copied tasks are only copied once
  - `children` parameter for `tasks.copy` API endpoint
  - `--children` flag for CLI `tasks copy` command

- **Copy Before Execution**
  - `copy_execution` parameter for `tasks.execute` API: Copy task before execution to preserve original task history
  - `copy_children` parameter: When `True` with `copy_execution=True`, also copy each direct child task with its dependencies
  - Response includes both `task_id` (copied task) and `original_task_id` (original task) when `copy_execution=True`
  - Combines `tasks.copy` and `tasks.execute` into a single API call for better user experience

- **Enhanced Task Deletion with Validation**
  - Physical deletion: Tasks are now physically removed from the database (not soft-deleted)
  - Conditional deletion: Tasks can only be deleted if all tasks (task itself + all children) are in `pending` status
  - Recursive child deletion: When deletion is allowed, all child tasks (including grandchildren) are automatically deleted
  - Dependency validation: Deletion is prevented if other tasks depend on the task being deleted
  - Detailed error messages: Returns specific error information when deletion fails:
    - Lists non-pending children with their statuses
    - Lists tasks that depend on the task being deleted
    - Indicates if the task itself is not pending
  - New TaskRepository methods:
    - `get_all_children_recursive()`: Recursively get all child tasks
    - `find_dependent_tasks()`: Find all tasks that depend on a given task (reverse dependencies)
    - `delete_task()`: Physically delete a task from the database
  - Comprehensive test coverage with 16 test cases covering all scenarios

- **Enhanced Task Update with Critical Field Validation**
  - Critical field protection: Three critical fields (`parent_id`, `user_id`, `dependencies`) are now strictly validated to prevent fatal errors
  - `parent_id` and `user_id`: Always rejected - these fields cannot be modified after task creation (task hierarchy and ownership are fixed)
  - `dependencies`: Conditional validation with four critical checks:
    1. Status check: Can only be updated when task is in `pending` status
    2. Reference validation: All dependency references must exist in the same task tree
    3. Circular dependency detection: Uses DFS algorithm to detect and prevent circular dependencies
    4. Execution check: Prevents updates if any dependent tasks are currently executing
  - Other fields: Can be updated freely without status restrictions (inputs, name, priority, params, schemas, status, result, error, progress, timestamps)
  - Comprehensive error reporting: All validation errors are collected and returned in a single response
  - New TaskRepository methods:
    - `update_task()`: Update task dependencies with validation
    - `update_task_name()`: Update task name
    - `update_task_priority()`: Update task priority
    - `update_task_params()`: Update executor parameters
    - `update_task_schemas()`: Update validation schemas
  - New utility module: `dependency_validator.py` with reusable validation functions
  - Comprehensive test coverage with 23 test cases covering all validation scenarios

### Fixed
- **Docker Executor Exit Code Extraction**
  - Fixed incorrect exit code handling: `container.wait()` returns `{"StatusCode": 0}` dict, not integer
  - Properly extract `StatusCode` from wait result dictionary
  - Fixed container removal logic to prevent duplicate cleanup calls
  - Improved cancellation handling before container start

- **WebSocket Executor Exception Handling**
  - Fixed `ConnectionClosed` exception construction in tests
  - Fixed `InvalidURI` exception construction in tests
  - Added proper `asyncio` import for timeout error handling
  - Improved error handling for WebSocket connection failures

- **SSH Executor Key File Validation**
  - Fixed key file validation in tests by properly mocking file system operations
  - Added proper handling for key file permissions and existence checks
  - Improved error messages for authentication failures

- **API Executor Infinite Polling Loop**
  - Fixed infinite polling loop when API calls fail repeatedly
  - Implemented production-grade retry logic with exponential backoff
  - Added circuit breaker pattern to stop polling after consecutive failures
  - Added total failure threshold to prevent resource waste
  - Improved error classification (retryable vs non-retryable errors)
  - Enhanced logging for better debugging in production environments

## [0.3.0] - 2025-11-30

### Added
- **Webhook Support for Task Execution**
  - Webhook callbacks for `tasks.execute` JSON-RPC endpoint (similar to A2A Protocol push notifications)
  - `WebhookStreamingContext` class for sending HTTP callbacks during task execution
  - Real-time progress updates sent to configured webhook URL
  - Configurable webhook settings:
    - `url` (required): Webhook callback URL
    - `headers` (optional): Custom HTTP headers (e.g., Authorization)
    - `method` (optional): HTTP method (default: POST)
    - `timeout` (optional): Request timeout in seconds (default: 30.0)
    - `max_retries` (optional): Maximum retry attempts (default: 3)
  - Automatic retry mechanism with exponential backoff for failed requests
  - Error handling: Client errors (4xx) are not retried, server errors (5xx) and network errors are retried
  - Webhook payload includes: protocol identifier, task status, progress, result, error, and timestamp
  - Update types: `task_start`, `progress`, `task_completed`, `task_failed`, `final`
  - Comprehensive test coverage for webhook functionality

- **Protocol Identification**
  - Added `protocol` field to all API responses to distinguish between execution modes
    - JSON-RPC endpoints return `"protocol": "jsonrpc"` in response
    - A2A Protocol endpoints include `"protocol": "a2a"` in metadata and event data
  - Enables clients to identify which protocol was used for task execution
  - Consistent protocol identification across streaming updates and webhook callbacks

- **Streaming Mode for JSON-RPC**
  - Streaming mode support for `tasks.execute` endpoint via `use_streaming` parameter
  - Real-time progress updates via Server-Sent Events (SSE) - returns `StreamingResponse` directly when `use_streaming=true`
  - `TaskStreamingContext` class for in-memory event storage
  - Consistent behavior with A2A Protocol streaming mode
  - Asynchronous task execution with immediate response

- **Examples Module**
  - New `examples` module for initializing example task data to help beginners get started
  - CLI command `examples init` for initializing example tasks in the database
  - `--force` flag to re-initialize examples even if they already exist
  - Example tasks demonstrate various features:
    - Tasks with different statuses (completed, failed, pending, in_progress)
    - Task trees with parent-child relationships
    - Tasks with different priorities
    - Tasks with dependencies
    - CrewAI task example (requires LLM key)
  - Auto-initialization: API server automatically initializes examples if database is empty on startup
  - Example user ID: `example_user` for demo tasks

- **LLM API Key Management**
  - Request header support: `X-LLM-API-KEY` header for demo/one-time usage
    - Simple format: `X-LLM-API-KEY: <api-key>` (provider auto-detected from model name)
    - Provider-specific format: `X-LLM-API-KEY: <provider>:<api-key>` (e.g., `openai:sk-xxx`, `anthropic:sk-ant-xxx`)
  - User configuration support via `llm-key-config` extension for multi-user scenarios
    - In-memory storage of user-specific LLM keys (never stored in database)
    - Support for multiple providers per user (provider-specific keys)
    - `LLMKeyConfigManager` singleton for managing user keys
  - LLM Key Context Manager: Thread-local context for LLM keys during task execution
  - LLM Key Injector: Automatic provider detection from model names and works configuration
  - Priority order: Request header > User config > Environment variables
  - Supported providers: OpenAI, Anthropic, Google/Gemini, Mistral, Groq, Cohere, Together, and more
  - Automatic environment variable injection for CrewAI/LiteLLM compatibility
  - Keys are never stored in database, only used during task execution

- **Documentation Updates**
  - Added `examples` command documentation in CLI guide
  - Added LLM API key management documentation in API server guide
  - Added LLM key header documentation in HTTP API reference
  - Updated examples documentation with quick start guide and example task structure
  - Comprehensive examples for using LLM keys with CrewAI tasks
  - Added webhook configuration documentation in HTTP API reference
  - Added protocol identification documentation

### Changed
- **Unified Task Execution Architecture**
  - Refactored task execution logic to unify behavior across A2A Protocol and JSON-RPC endpoints
  - Added `TaskExecutor.execute_task_by_id()` method for executing tasks by ID with automatic dependency handling
  - Moved dependency collection and subtree building logic from protocol handlers to core `TaskExecutor` layer
  - Unified execution modes:
    - **Root task execution**: Executes the entire task tree when a root task is specified
    - **Child task execution**: Automatically collects all dependencies (including transitive) and executes the task with required dependencies
  - Both A2A Protocol and JSON-RPC now use the same core execution methods for consistent behavior
  - Protocol handlers are now thin wrappers around core execution logic, making the system more library-friendly
  - Improved code reusability and maintainability by centralizing execution logic in `TaskExecutor`

- **Task Re-execution Support**
  - Added support for re-executing failed tasks via `tasks.execute` endpoint
  - Failed tasks can now be re-executed by calling `tasks.execute` with the failed task's ID
  - When re-executing a task, all its dependency tasks are also re-executed (even if they are completed) to ensure consistency
  - Only `pending` and `failed` status tasks are executed; `completed` and `in_progress` tasks are skipped unless marked for re-execution
  - Dependency satisfaction logic updated to allow completed tasks marked for re-execution to satisfy dependencies (results are available)
  - Newly created tasks (status: `pending`) are not marked for re-execution and execute normally

- **Task Execution Order Clarification**
  - **Important**: Parent-child relationships (`parent_id`) are now explicitly documented as **organizational only** and do NOT affect execution order
  - **Only dependencies (`dependencies`) determine execution order** - a task executes when its dependencies are satisfied
  - This clarification ensures developers understand that task tree structure (parent-child) is separate from execution order (dependencies)
  - Updated all documentation to clearly distinguish between organizational relationships and execution dependencies

- **CLI Command Improvements**
  - `serve` command now accepts options directly (e.g., `apflow serve --port 8000`) without requiring `start` subcommand
  - `serve start` subcommand still works for backward compatibility
  - Improved command structure and user experience

- **CORS Support**
  - Added CORS middleware to API server for cross-origin requests
  - Default configuration allows `localhost:3000`, `localhost:3001` and common development ports
  - Configurable via `APFLOW_CORS_ORIGINS` environment variable (comma-separated list)
  - Development mode: `APFLOW_CORS_ALLOW_ALL=true` to allow all origins
  - Supports credentials, all HTTP methods, and all headers

- **API Architecture Refactoring**
  - Moved documentation routes (`/docs`, `/openapi.json`) to `api/routes/docs.py` for better code organization
  - Consolidated OpenAPI schema generation logic into `DocsRoutes` class
  - Improved separation of concerns: route handlers in `api/routes/`, documentation tools in `api/docs/`
  - All custom routes (tasks, system, docs) are now defined in a unified structure

- **SSE Streaming Simplification**
  - SSE streaming is now handled directly by `tasks.execute` with `use_streaming=true`
  - When `use_streaming=true`, `tasks.execute` returns a `StreamingResponse` with Server-Sent Events
  - Simplified API design: one endpoint (`tasks.execute`) handles both regular POST and SSE streaming modes
  - Webhook callbacks remain independent and can be used with either response mode

### Fixed
- **Test Infrastructure**
  - Consolidated database session management in `conftest.py` with global `use_test_db_session` fixture
  - All tests now use isolated in-memory DuckDB databases to prevent data pollution
  - Removed dependency on persistent database files for testing
  - Improved test isolation and reliability
  - Fixed `test_webhook_config_validation` test by using `patch.object()` for TaskTracker singleton
  - Updated `test_jsonrpc_tasks_execute_with_streaming` to correctly parse SSE stream responses
  - Added comprehensive test coverage for documentation routes (`/docs` and `/openapi.json`) with 14 test cases

- **Documentation Corrections**
  - Fixed incorrect command examples in README.md and docs:
    - Corrected `run my_batch` to proper `run flow --tasks` or `run flow executor_id` format
    - Corrected `run flow example_flow` to proper executor ID format
    - Removed non-existent `list-flows` command from documentation
  - Ensured all command examples in documentation are accurate and testable
  - Updated SSE streaming documentation to reflect direct integration with `tasks.execute`

## [0.2.0] - 2025-11-21

### Added
- **Task Tree Validation**
  - Circular dependency detection using DFS algorithm in `TaskCreator.create_task_tree_from_array()`
  - Single task tree validation ensuring all tasks are in the same tree structure
  - Validation that only one root task exists
  - Verification that all tasks are reachable from root task via parent_id chain
  - Dependent task inclusion validation (ensures all tasks that depend on tasks in the tree are included)
  - Comprehensive test coverage for circular dependency scenarios

- **Task Copy Functionality**
  - `TaskCreator.create_task_copy()` method for creating executable copies of task trees
  - Automatic inclusion of dependent tasks (including transitive dependencies) when copying
  - Special handling for failed leaf nodes (filters out pending dependents)
  - Minimal subtree construction to include only required tasks
  - Task copy fields in `TaskModel`: `original_task_id` (links copy to original), `task_tree_id` (for tree grouping), `origin_type` (tracks origin type), and `has_references` (indicates if task has copies)
  - API endpoint `tasks.copy` via JSON-RPC `/tasks` endpoint
  - CLI command `tasks copy <task_id>` for copying task trees
  - Comprehensive test coverage for task copy functionality

## [0.1.0] - 2025-11-19

### Added
- **Task Orchestration Engine**
  - `TaskManager`: Core task orchestration with dependency management and tree execution
  - `TaskRepository`: Data access layer for task CRUD operations
  - `TaskModel`: Task definition model with support for custom fields via inheritance
  - Task tree structure with parent-child relationships
  - Priority-based task execution
  - Dependency resolution and satisfaction checking

- **A2A Protocol Integration**
  - A2A Protocol as the standard communication protocol
  - Task definition (TaskModel) vs execution instance (A2A Task) separation
  - Context ID mapping: TaskModel.id → A2A Task.context_id
  - Uses A2A SDK's InMemoryTaskStore for task execution instances
  - TaskModel persistence handled by TaskRepository (separate from A2A TaskStore)

- **API Service Layer**
  - A2A server implementation with Starlette
  - Custom A2A Starlette application with system routes
  - Task management APIs: `/system/task_create`, `/system/task_get`, `/system/task_update`, `/system/task_delete`
  - Optional JWT authentication middleware
  - Support for custom TaskModel via `task_model_class` parameter
  - Environment variable support for custom TaskModel loading (`APFLOW_TASK_MODEL_CLASS`)

- **Storage Module**
  - SQLAlchemy-based storage with DuckDB (default) and PostgreSQL support
  - Automatic table creation on first use
  - Configurable table name via `APFLOW_TASK_TABLE_NAME` (default: `apflow_tasks`)
  - Session factory with sync/async support

- **Custom TaskModel Support**
  - Users can define custom TaskModel subclasses with additional fields
  - TaskRepository supports custom TaskModel classes
  - Custom fields automatically handled in task creation APIs
  - Example: `MyTaskModel(TaskModel)` with `project_id`, `department` fields

- **Event Queue Bridge**
  - Streaming callbacks integration with A2A Protocol EventQueue
  - Real-time task execution progress updates

- **Base Task Infrastructure**
  - `BaseTask`: Optional base class for executable tasks with common implementations
  - Input validation utilities (`get_input_schema`, `validate_input_schema`, `check_input_schema`)
  - Cancellation support via `cancellation_checker` callback
  - Streaming context support for progress updates
  - Support for Pydantic BaseModel or JSON schema dict for input validation

- **Executor Extension System**
  - Unified `ExtensionRegistry` for executor registration and discovery
  - `@executor_register()` decorator for automatic registration
  - Category and type-based executor discovery
  - Extension system supporting executors, storage, hooks, and tools
  - Globally unique ID-based extension lookup

- **Built-in Executors**
  - `AggregateResultsExecutor`: Aggregates dependency task results into structured format
  - `SystemInfoExecutor`: Safe system resource queries (CPU, memory, disk) with predefined commands
  - `CommandExecutor`: Shell command execution (stdio extension)
  - `CrewaiExecutor`: LLM-based agent crew execution via CrewAI with token usage tracking
  - `BatchCrewaiExecutor`: Atomic batch execution of multiple crews with result merging

### Infrastructure
- Project structure with `src-layout`
- `pyproject.toml` with optional dependencies (`[a2a]`, `[crewai]`, `[cli]`, `[postgres]`, `[all]`)
- Comprehensive documentation in `docs/` directory
- Test suite with pytest fixtures
- Modular architecture separating core from optional features

