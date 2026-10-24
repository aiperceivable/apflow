# apflow

**AI-Perceivable Distributed Orchestration**

apflow is a distributed task orchestration engine where every capability is AI-perceivable — discoverable, understandable, and invocable by AI agents through the apcore module standard.

## The Tesla Analogy

Think of Tesla's Full Self-Driving (FSD):

```
Tesla = Traditional car systems (brakes, steering, battery management)
        + FSD (the AI brain that perceives and controls everything)

        The braking system doesn't need to be "smart."
        But it must be perceivable and controllable by FSD.

apflow = Traditional orchestration (dependency graphs, priority scheduling,
         distributed coordination)
        + apcore (makes every capability AI-perceivable)

        Task orchestration doesn't need AI.
        But it must be perceivable and invocable by AI agents.
```

**Tesla doesn't build a competitor to FSD — it builds the best car that FSD can control. apflow doesn't build AI agents — it builds the best orchestration engine that AI agents can invoke.**

## What apflow IS and IS NOT

| apflow IS | apflow IS NOT |
|---|---|
| A distributed orchestration engine | An AI agent framework |
| AI-perceivable via apcore | An AI/LLM product |
| Deterministic, reliable task coordination | A competitor to LangGraph/CrewAI |
| The "car systems" that AI agents control | The "FSD brain" itself |

## Requirements

- Python >= 3.11

## Install

```bash
pip install apflow
```

## Quick Start

```python
from apflow import TaskManager, create_session
from apflow.app import create_app

# One line to bootstrap the full stack
app = create_app()

# Start A2A server — AI agents can now discover and invoke orchestration
from apcore_a2a import serve
from apcore import Executor
from apflow.bridge.protocol import PublicExecutor

serve(PublicExecutor(Executor.from_registry(app.registry)), name="apflow")
```

```bash
# Or from the command line
apflow serve              # A2A HTTP server
apflow serve --explorer   # With Explorer UI
apflow serve --all        # Unified REST + A2A + MCP on one port (/, /a2a, /mcp)
apflow worker --db <postgres-url>  # Start distributed worker node (requires PostgreSQL)
apflow rest               # REST/HTTP API + OpenAPI docs at /docs (default :8080)
apflow scheduler          # Run the internal poll-based scheduler (foreground process)
apflow dispatcher --db <database-url>  # Execute durably submitted roots
apflow task submit --task-id <root-id> # Accept a root for background execution
apflow run get --run-id <root-id>      # Inspect a Run and its retry lineage
apflow run retry --run-id <old-root-id> --idempotency-key <operation-key>
apflow budget scope --scope <name> --limit-tokens 10000
apflow usage events --after-index 0 --limit 100
apflow mcp                # MCP server (stdio, for Claude/Cursor)
apflow mcp --transport streamable-http --approval  # HTTP + async human-approval workflow
apflow info               # Show version, config, and registered modules
```

## Core Capabilities

### Task Orchestration (Dual Model: Structure Tree + Execution DAG)

apflow uses a **dual model** — structure tree (`parent_id`) for organization and execution DAG (`dependencies`) for ordering. This is not redundancy; each serves different operations:

```
parent_id    → Structure: copy, link, archive, progress aggregation
dependencies → Execution: parallel scheduling, fan-in, result injection
```

```python
tasks = [
    {"id": "a", "name": "Step A", "priority": 1},
    {"id": "b", "name": "Step B", "priority": 1},
    {"id": "merge", "name": "Merge", "parent_id": "a", "priority": 2,
     "dependencies": [{"id": "a"}, {"id": "b"}]},  # fan-in: waits for both
]
tree = await task_creator.create_task_tree_from_array(tasks)
await task_manager.distribute_task_tree(tree)
```

### Five Task Creation Modes

| Mode | Method | When to use |
|------|--------|-------------|
| **Create** | `create_task_tree_from_array()` | Build a new workflow from scratch |
| **Link** | `from_link()` | Reference a completed workflow (read-only, low storage) |
| **Copy** | `from_copy()` | Clone a workflow with modifications (re-run with new params) |
| **Archive** | `from_archive()` | Freeze an existing completed workflow in place (audit, compliance) |
| **Mixed** | `from_mixed()` | Partial copy + partial link (re-run only changed steps) |

See [Task Orchestration Architecture](docs/architecture/task-orchestration.md) for the full design rationale.

### Durable Execution

Durable submission receipts, immutable accepted Runs, classified retries with
backoff, and circuit breakers per executor. `run retry` creates a new Run and
reuses confirmed completed results; submit its new root to execute it. Historical
trees cannot be resumed in place. Checkpoint restoration is executor-specific
within an active Run. Unknown remote outcomes require receipt verification and
explicit replay authorization.

CLI module commands use the local database configured by `APFLOW_DATABASE_URL`.
For standalone CLI/MCP/A2A submission, run a dispatcher against that same database;
REST and the unified server already run one during their application lifespan.
See the [durable submission and recovery guide](docs/features/rest-api.md#durable-submission-and-recovery).

### Cost Governance

Shared token-budget reservations, evidenced reconciliation, model downgrade
chains, and policy enforcement (block/downgrade/notify). Immutable usage events
support delivery through `usage events` and `usage ack`. Consumers commit and
deduplicate receipts with their business writes before acknowledging; pricing,
credits, refunds, and wallets belong to the consuming application.
See [budget and usage operations](docs/features/cost-governance.md#public-budget-and-usage-operations).

### Distributed Coordination

Leader election, task leasing, worker management — scales from single process to multi-node cluster.

### AI-Perceivable (via apcore)

Every orchestration capability is automatically exposed as an apcore Module:
- **MCP** — AI agents (Claude, Cursor) discover and call orchestration tools
- **A2A** — Other services invoke orchestration via HTTP
- **CLI** — Humans operate orchestration from the terminal

See [MCP and A2A operations](docs/features/apcore-bridge.md#mcp-and-a2a-operations)
for discovery, request examples, embedded servers, and accounting constraints.

## Architecture

```
AI Agents / Services / Humans
    ↓ discover & invoke
    ↓
┌──────────────────────────────────────────────┐
│  apflow — AI-Perceivable Distributed         │
│           Orchestration Engine                │
│                                              │
│  ┌── Protocol Exposure (apcore) ───────────┐ │
│  │ apcore-mcp · apcore-a2a · apcore-cli    │ │
│  │              ↕                          │ │
│  │       apcore Registry (Modules)         │ │
│  └──────────────┬──────────────────────────┘ │
│                 │                             │
│  ┌──────────────▼──────────────────────────┐ │
│  │  Orchestration Core                     │ │
│  │  TaskManager · TaskCreator · Scheduler  │ │
│  │  Dependency Graphs · Priority · DAG     │ │
│  ├─────────────────────────────────────────┤ │
│  │  Durability    │  Governance            │ │
│  │  Checkpoint    │  Budget · Policy       │ │
│  │  Retry         │  Model Downgrade       │ │
│  │  Circuit Break │  Usage Reporting       │ │
│  ├─────────────────────────────────────────┤ │
│  │  Infrastructure                         │ │
│  │  SQLite/PostgreSQL · Distributed Runtime│ │
│  │  ConfigManager · Adapters               │ │
│  └─────────────────────────────────────────┘ │
└──────────────────────────────────────────────┘
```

apcore is not a separate layer — it's embedded inside apflow as the mechanism that makes orchestration capabilities AI-perceivable (like a CAN bus makes car systems perceivable to FSD).

## Built-in Executors

| Executor | Purpose |
|----------|---------|
| RestExecutor | HTTP/REST API calls (example executor) |
| AggregateResultsExecutor | Combine results from dependency tasks |
| ApFlowApiExecutor | Inter-instance orchestration (cluster) |
| SendEmailExecutor | Email notifications |

These are examples and utilities. The real executors are your AI agents, business logic, or any `ExecutableTask` implementation.

## Documentation

- [PRD](docs/prd.md) — Product requirements
- [Tech Design](docs/tech-design.md) — Architecture and design
- [Feature Specs](docs/features/overview.md) — Implementation specifications

## Contributing

Contributions welcome. Please open an issue or PR on GitHub.

### Development setup

```bash
# Install the package with development dependencies
# (pytest, ruff, black, pre-commit, apdev, ...).
pip install -e ".[dev]"

# Install the git hooks. Run this with the same interpreter that has the
# dev dependencies installed, so the generated hook points at the right Python.
pre-commit install
```

The pre-commit hooks call the `apdev` CLI (`check-chars`, `check-imports`) and
`ruff`; all of them come from the `dev` extra above. If you manage Python with
`pyenv`, run `pyenv rehash` after installing so the `apdev` shim lands on `PATH`.

## License

Apache-2.0

## Links

- **Website**: [aiperceivable.com](https://aiperceivable.com)
- **GitHub**: [aiperceivable/apflow](https://github.com/aiperceivable/apflow)
- **PyPI**: [apflow](https://pypi.org/project/apflow/)
