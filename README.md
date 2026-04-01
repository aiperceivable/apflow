# apflow

**AI Agent Production Middleware**

apflow makes any AI agent production-ready — regardless of which framework built it — by providing durable execution, cost governance, and automatic protocol exposure via the apcore ecosystem.

## What apflow IS and IS NOT

| apflow IS | apflow IS NOT |
|---|---|
| Production middleware for AI agents | An agent framework (use LangGraph/CrewAI/etc.) |
| Framework-agnostic reliability layer | A replacement for your existing stack |
| Cost governance and budget enforcement | An LLM routing layer (use LiteLLM/Portkey) |
| A bridge to MCP and A2A protocols | An observability platform (use Langfuse) |

## Requirements

- Python >= 3.11

## Install

```bash
pip install apflow
```

This installs core + apcore + MCP + A2A + CLI. No extras needed for full functionality.

Optional:

```bash
pip install apflow[postgres]    # PostgreSQL for distributed deployment
pip install apflow[all]         # All optional executors (SSH, Docker, Email, etc.)
```

## Quick Start

```python
from apflow import TaskManager, create_session
from apflow.bridge import create_apflow_registry
from apflow.durability import RetryPolicy, CheckpointManager, CircuitBreakerRegistry
from apflow.governance import BudgetManager, PolicyEngine, CostPolicy, PolicyAction

# 1. Create database session (SQLite by default, zero config)
session = create_session()

# 2. Create task manager with durability and governance
task_manager = TaskManager(
    session,
    checkpoint_manager=CheckpointManager(session),
    circuit_breaker_registry=CircuitBreakerRegistry(),
)

# 3. Register as apcore modules (auto-exposes via MCP, A2A, CLI)
registry = create_apflow_registry(task_manager, task_creator, task_repository)

# 4. Start MCP server (AI agents can now discover and call apflow)
from apcore_mcp import serve
serve(registry, transport="stdio")
```

## Core Features

### Durable Execution (F-003)

Checkpoint/resume for long-running tasks. Retry with configurable backoff. Circuit breaker for fault isolation.

```python
from apflow.durability import RetryPolicy, BackoffStrategy

policy = RetryPolicy(
    max_attempts=5,
    backoff_strategy=BackoffStrategy.EXPONENTIAL,
    backoff_base_seconds=2.0,
)
```

### Cost Governance (F-004)

Token budget management with automatic model downgrade when budgets are approached.

```python
from apflow.governance import PolicyEngine, CostPolicy, PolicyAction

engine = PolicyEngine()
engine.register_policy(CostPolicy(
    name="auto-downgrade",
    action=PolicyAction.DOWNGRADE,
    threshold=0.8,
    downgrade_chain=["claude-opus-4", "claude-sonnet-4", "claude-haiku-4"],
))
```

### apcore Module Bridge (F-002)

One registration, three protocols. All apflow capabilities automatically exposed via MCP, A2A, and CLI.

```python
from apflow.bridge import create_apflow_registry

registry = create_apflow_registry(task_manager, task_creator, task_repository)

# MCP — AI agent tool integration
from apcore_mcp import serve
serve(registry, transport="streamable-http", port=8000)

# A2A — Internal network service
from apcore_a2a import serve as a2a_serve
a2a_serve(registry, name="apflow", url="http://localhost:9000")

# CLI — Human operation
from apcore_cli import create_cli
cli = create_cli()
```

## Storage

- **SQLite** (default): Zero config, WAL mode, in-memory for tests
- **PostgreSQL**: For distributed/production deployments

```python
# SQLite (default)
session = create_session()

# SQLite in-memory (testing)
session = create_session(path=":memory:")

# PostgreSQL (production)
session = create_session(connection_string="postgresql://user:pass@host/db")
```

## Architecture

```
Protocol Layer (apcore ecosystem)
  apcore-mcp  |  apcore-a2a  |  apcore-cli

Module Standard (apcore)
  Registry  |  Executor  |  ACL  |  Middleware

apflow v2 (this project)
  Durable Execution  |  Cost Governance  |  Module Bridge
  Task Orchestration Engine (TaskManager, TaskCreator)
  Executors (REST, SSH, Docker, Email, ...)
  Storage (SQLite / PostgreSQL)

Agent Frameworks (bring your own)
  LangGraph  |  CrewAI  |  OpenAI Agents  |  Any
```

## Built-in Executors

| Executor | Purpose | Extra |
|----------|---------|-------|
| RestExecutor | HTTP/REST API calls | core |
| AggregateResultsExecutor | Combine results from dependency tasks | core |
| ApFlowApiExecutor | Inter-instance API calls (cluster) | core |
| SendEmailExecutor | Email via SMTP or Resend | [email] |

These are examples. The real executors are your AI agents, business logic, or any `ExecutableTask` implementation.

## Documentation

- [PRD](docs/apflow-v2/prd.md) — Product requirements
- [Tech Design](docs/apflow-v2/tech-design.md) — Architecture and design
- [Feature Specs](docs/features/) — Implementation specifications

## Contributing

Contributions welcome. Please open an issue or PR on GitHub.

## License

Apache-2.0

## Links

- **Website**: [aiperceivable.com](https://aiperceivable.com)
- **GitHub**: [aiperceivable/apflow](https://github.com/aiperceivable/apflow)
- **PyPI**: [apflow](https://pypi.org/project/apflow/)
