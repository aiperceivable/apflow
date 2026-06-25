# High-Quality Code Specification – Simplicity, Readability, and Maintainability First

## Project Overview
`apflow` is **AI-Perceivable Distributed Orchestration** — a task orchestration engine where every capability is AI-perceivable via the apcore module standard.

**The Tesla Analogy:** Tesla's braking system doesn't need to be "smart," but it must be perceivable and controllable by FSD. Similarly, apflow's task orchestration doesn't need AI, but it must be perceivable and invocable by AI agents.

apflow does not implement AI intelligence — it implements deterministic, reliable orchestration that is natively AI-perceivable. AI agents discover, understand, and invoke apflow's capabilities through MCP/A2A/CLI via the embedded apcore module standard.

### Dual Model: Structure Tree + Execution DAG
- `parent_id` → Structure tree: organizational hierarchy (supports copy, link, archive, mixed, progress)
- `dependencies` → Execution DAG: ordering constraints (supports parallel, fan-in, result injection)
- These are NOT redundant — the tree supports 5 operations, the DAG supports 1. See `docs/architecture/task-orchestration.md`.

### Five Task Creation Modes
- **Create** (from_array), **Link** (read-only ref), **Copy** (modifiable clone), **Archive** (frozen snapshot), **Mixed** (partial copy + link)

### v2 Architecture (0.20.0)
- **core/execution/**: TaskManager — dependency graphs, priority scheduling, parallel execution
- **bridge/**: apcore Module registration (auto-discovers executors, exposes via MCP/A2A/CLI)
- **durability/**: Checkpoint/resume, retry with backoff, circuit breaker
- **governance/**: Token budget management, cost policy engine, model downgrade chains
- **core/storage/**: SQLite (default) / PostgreSQL, SQLAlchemy ORM
- **core/distributed/**: Leader election, task leasing, worker management
- **extensions/**: Core executors (REST, AggregateResults, ApFlowApi, Email)
- **app.py**: Bootstrap factory — `create_app()` initializes full stack
- **cli.py**: Entry points — `apflow serve`, `apflow mcp`, `apflow info`

### Key Dependencies (embedded, not external layers)
- `apcore` — Module standard embedded inside apflow (like CAN bus in a car)
- `apcore-mcp` — Protocol adapter: exposes modules as MCP tools
- `apcore-a2a` — Protocol adapter: exposes modules as A2A skills
- `apcore-cli` — CLI generation (humans operate orchestration)

## Core Principles
- Prioritize **simplicity, readability, and maintainability** above all.
- Avoid premature abstraction, optimization, or over-engineering.
- Code should be understandable in ≤10 seconds; favor straightforward over clever.
- Always follow: Understand → Plan → Implement minimally → Test/Validate → Commit.

## Python Code Quality

### Readability
- Use precise, full-word names (standard abbreviations only when conventional).
- Functions ≤50 lines, single responsibility, verb-named.
- Avoid obscure tricks, heavy comprehensions, excessive *args/**kwargs, unnecessary decorators.
- Break complex logic into small, well-named helpers.

### Types (Mandatory)
- Full type annotations everywhere.
- Avoid `Any` except for dynamic/external data.
- Prefer `dataclass`, `TypedDict`, `Protocol`, `NewType`.

### Design
- Favor functional style + data classes; minimize inheritance.
- Composition > inheritance; use Protocols/ABCs only for true interfaces.
- No circular imports.
- Dependency injection for config, logging, DB, etc.

### Errors & Resources
- Explicit exception handling; no bare `except:`.
- Context managers for files/DB/connections.
- Validate/sanitize all public inputs.

### Logging
- Use `logging.info` for key paths.
- `logging.error` + context for exceptions.
- No `print()` in production/debug code.

### Testing
- Unit tests in `tests/`, ≥90% coverage on core logic.
- Name: `test_<unit>_<behavior>`.
- Never change prod code without/updating tests.

### Formatting & Linting
- After changes, always run:
  - ruff check --fix .
  - black .
  - pyright .
- Zero errors/warnings before commit.

### Security & Performance
- Never hardcode secrets; use env/config.
- Validate/sanitize inputs.
- Avoid unjustified quadratic+ complexity in hot paths.

## General Guidelines
- English ONLY for comments, docstrings, logs, errors, commit messages.
- Fully understand surrounding code before changes.
- Do not generate unnecessary documentation, examples, stubs, or bloated `__init__.py` files unless explicitly requested.