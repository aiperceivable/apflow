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
serve(app.registry, name="apflow")
```

```bash
# Or from the command line
apflow serve              # A2A HTTP server
apflow serve --explorer   # With Explorer UI
apflow serve --cluster    # Distributed cluster mode (requires PostgreSQL)
apflow worker --db ...    # Start worker node
apflow mcp                # MCP server (stdio, for Claude/Cursor)
apflow mcp --transport streamable-http --approval  # HTTP + async human-approval workflow
apflow info               # Show registered modules
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
| **Link** | `from_link()` | Reference a completed workflow (read-only, zero storage) |
| **Copy** | `from_copy()` | Clone a workflow with modifications (re-run with new params) |
| **Archive** | `from_archive()` | Freeze a workflow snapshot (audit, compliance) |
| **Mixed** | `from_mixed()` | Partial copy + partial link (re-run only changed steps) |

See [Task Orchestration Architecture](docs/architecture/task-orchestration.md) for the full design rationale.

### Durable Execution

Checkpoint/resume, retry with configurable backoff, circuit breaker per executor.

### Cost Governance

Token budget management, model downgrade chains, policy engine (block/downgrade/notify).

### Distributed Coordination

Leader election, task leasing, worker management — scales from single process to multi-node cluster.

### AI-Perceivable (via apcore)

Every orchestration capability is automatically exposed as an apcore Module:
- **MCP** — AI agents (Claude, Cursor) discover and call orchestration tools
- **A2A** — Other services invoke orchestration via HTTP
- **CLI** — Humans operate orchestration from the terminal

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

## License

Apache-2.0

## Links

- **Website**: [aiperceivable.com](https://aiperceivable.com)
- **GitHub**: [aiperceivable/apflow](https://github.com/aiperceivable/apflow)
- **PyPI**: [apflow](https://pypi.org/project/apflow/)
