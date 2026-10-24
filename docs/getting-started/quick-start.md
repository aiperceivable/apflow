---
description: Quick-start guide for running apflow servers, creating tasks, reusing workflows, and exposing tools to AI agents.
---

# Quick Start

Get running in 5 minutes.

## 1. Start a Server

```bash
# A2A HTTP server (for services and agents)
apflow serve --explorer

# Unified REST + A2A + MCP server
apflow serve --all

# Or MCP server (for Claude/Cursor)
apflow mcp
```

Open http://localhost:8000/explorer to see all available tools.

### Docker Compose

The included Compose file starts the unified server on container port `8000`:

```bash
docker compose up -d

# Optional: publish a different host port
APFLOW_API_PORT=9000 docker compose up -d

# Optional: use PostgreSQL storage
APFLOW_DATABASE_URL=postgresql+asyncpg://user:pass@db-host/apflow docker compose up -d
```

For distributed execution, start worker nodes separately with
`apflow worker --db <postgres-url>`.

## 2. Create and Run Tasks (Python)

```python
import asyncio
from apflow.app import create_app
from apflow.adapters.function_executor import function_executor

# Register a custom executor
@function_executor(id="greet", description="Greet someone")
async def greet(inputs: dict) -> dict:
    return {"message": f"Hello, {inputs['name']}!"}

async def main():
    app = create_app(connection_string="sqlite:///:memory:")

    # Create a task
    tree = await app.task_creator.create_task_tree_from_array([
        {"name": "Say Hello", "inputs": {"name": "World"},
         "params": {"executor_id": "greet"}}
    ])

    # Execute it
    await app.task_manager.distribute_task_tree(tree)

    # Check result
    task = await app.task_repository.get_task_by_id(tree.task.id)
    print(f"Status: {task.status}, Result: {task.result}")

asyncio.run(main())
```

## 3. Task Trees (Multi-Step Workflows)

```python
tasks = [
    {"id": "fetch_a", "name": "Fetch A", "priority": 1,
     "params": {"executor_id": "rest_executor"},
     "inputs": {"url": "https://api.example.com/a", "method": "GET"}},

    {"id": "fetch_b", "name": "Fetch B", "priority": 1,
     "params": {"executor_id": "rest_executor"},
     "inputs": {"url": "https://api.example.com/b", "method": "GET"}},

    {"id": "merge", "name": "Merge Results", "priority": 2,
     "parent_id": "fetch_a",
     "dependencies": [{"id": "fetch_a"}, {"id": "fetch_b"}],
     "params": {"executor_id": "aggregate_results_executor"}},
]

tree = await app.task_creator.create_task_tree_from_array(tasks)
await app.task_manager.distribute_task_tree(tree)
```

`fetch_a` and `fetch_b` run in parallel (same priority, no mutual dependency). `merge` waits for both to complete.

## 4. Reuse Workflows

```python
# Copy a completed workflow with new parameters
copy_tree = await app.task_creator.from_copy(
    original_task, inputs={"url": "https://new-api.example.com"}
)

# Link to a completed workflow (read-only, without duplicating payload data)
link_tree = await app.task_creator.from_link(original_task)

# Archive a workflow in place
archive_tree = await app.task_creator.from_archive(original_task)
```

## 5. Durable Submission and Retry (CLI)

CLI module commands use local storage, not the HTTP server. Select the same
database as your service or standalone dispatcher:

Ensure `storage.connection_string` and `DATABASE_URL` do not select a different
database; they take precedence over `APFLOW_DATABASE_URL`. See
[configuration precedence](../architecture/configuration.md#service-deployment-configuration).

```bash
export APFLOW_DATABASE_URL=sqlite:////absolute/path/apflow.db

# Run in another terminal if no REST/unified server dispatcher is active.
apflow dispatcher --db "$APFLOW_DATABASE_URL"
```

Create an idempotent draft using the built-in local aggregate executor:

```bash
apflow task create --name example --idempotency-key example-create-1 --input - <<'JSON'
{"params":{"executor_id":"aggregate_results_executor"}}
JSON

# Recover the creation response if it was lost.
apflow task receipt --idempotency-key example-create-1 --shape task

# Use id from the creation receipt. This root ID is also the accepted Run ID.
apflow task submit --task-id ROOT_ID
apflow task get --task-id ROOT_ID
apflow run get --run-id ROOT_ID
```

For a failed, cancelled, or uncertain Run, create a new retry Run and submit it:

```bash
apflow run retry --run-id OLD_ROOT_ID --idempotency-key example-retry-1

# Use run_id from the retry receipt; retry creation alone does not execute it.
apflow task submit --task-id NEW_RUN_ID
```

Completed reusable results become frozen snapshots without inherited charges.
Supply optional `task_ids` through JSON input to select retry seeds:

```bash
apflow run retry --run-id OLD_ROOT_ID --idempotency-key selected-retry-1 --input - <<'JSON'
{"task_ids":["FAILED_TASK_ID"]}
JSON
```

Use `--input FILE` or `--input -` for object/array values; the current CLI's
individual object/array options are text and do not decode JSON strings.
Dependencies, affected downstream tasks, and structural ancestors determine the full scope.
The old Run retains its outcomes. Ordinary Copy resets execution state, so it
does not implement selective retry.

If a remote outcome is unknown, first verify the remote receipt or the stable
business idempotency key. Replay requires explicit authorization with
`--replay-safe --evidence-ref receipt:operation-1`; authenticated services require
an administrator. There is no generic tree `resume`, and `task reconcile` rejects
in-place recovery. See [REST recovery](../features/rest-api.md#durable-submission-and-recovery)
for HTTP examples.

Inspect the shared budget and usage delivery surfaces with:

```bash
apflow budget scope --scope experiment-1 --limit-tokens 10000
apflow budget scope --scope experiment-1
apflow usage events --after-index 0 --limit 100

# Only after committing the consuming application's receipt/business transaction.
apflow usage ack --event-id EVENT_ID
```

Configuring a scope does not automatically enroll tasks. See
[budget and usage operations](../features/cost-governance.md#public-budget-and-usage-operations)
for task binding, bounded executors, reconciliation, and event deduplication.
Pricing, credits, and wallets belong to the consuming application.

## 6. Let AI Agents Use apflow

Registered operations are exposed as MCP tools and A2A skills with the same IDs:

```
apflow.task.create_tree   — Create multi-step workflow
apflow.task.execute       — Execute a task
apflow.task.submit        — Accept a root for durable background execution
apflow.task.receipt       — Recover an idempotent creation receipt
apflow.run.get            — Inspect an accepted Run
apflow.run.retry          — Create a new retry Run
apflow.budget.scope       — Configure or inspect a shared token budget
apflow.budget.reconcile   — Settle usage using provider evidence
apflow.usage.events       — Read immutable consumption events
apflow.usage.ack          — Acknowledge a durably processed event
apflow.task.copy          — Clone with modifications
apflow.task.link          — Reference completed workflow
apflow.task.cancel        — Cancel running tasks
apflow.task.tree          — View full tree structure
apflow.schedule.set       — Configure a task's schedule
... plus other management modules and discovered executors
```

Start `apflow mcp` for an MCP client such as Claude/Cursor, or `apflow serve`
for A2A. Use `apflow serve --all` to expose REST, MCP, and A2A together. Discovery
and invocation follow the configured ACL and approval policy; approval-required
skills such as `apflow.budget.reconcile` are omitted from the public A2A card.

An A2A task ID identifies a protocol invocation. Use the module's returned
`run_id` to inspect the apflow Run. Completion of a `task.submit` call means the
workflow was accepted; observe `run.get` for its execution outcome. Standalone
servers need a separate dispatcher against the same database for durable work.

See [MCP and A2A operations](../features/apcore-bridge.md#mcp-and-a2a-operations)
for endpoints, request examples, authenticated discovery, accounting precision,
and embedded server adapters.

## Next Steps

- [Core Concepts](concepts.md) — Understand the dual model (tree + DAG)
- [Task Orchestration Guide](../guides/task-orchestration.md) — Advanced patterns
- [Architecture](../architecture/task-orchestration.md) — Full design rationale
