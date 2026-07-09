---
description: Current guide to apflow feature specifications, capability groups, and recommended reading paths.
---

# Feature Specifications

This section contains implementation-oriented feature specifications for apflow.
They explain why major capabilities exist, which modules they touch, and which
tests cover important behavior.

Use these pages as design and maintenance context. For end-user workflow
documentation, start with [Task Orchestration](../guides/task-orchestration.md),
[REST / HTTP API](rest-api.md), and [Built-in Scheduler](scheduler.md).

## Current Capability Areas

### Protocol Exposure

apflow capabilities are registered once in the apcore Registry and exposed through
multiple protocol faces:

- [apcore Bridge](apcore-bridge.md) describes module registration and protocol reuse.
- [REST / HTTP API](rest-api.md) describes the registry-driven REST/OpenAPI surface,
  unified server, streaming, auth, webhook trigger, and scheduler integration.
- [Function Executor](function-executor.md) describes the decorator path for turning
  Python functions into executable apflow modules.

### Orchestration and Scheduling

The execution model is built around task trees, dependency DAGs, reusable
workflows, and scheduled run instances:

- [TaskCreator Relaxation](task-creator-relaxation.md) explains multi-root task
  forests and why `create_task_tree_from_array()` allows them.
- [Built-in Scheduler](scheduler.md) explains the direct-DB poll loop,
  schedule modules, webhook triggers, distributed dispatch-only mode, and run
  history.

### Reliability and Governance

Durability and budget controls live in the execution path, so they apply whether
tasks are invoked by Python, CLI, REST, MCP, A2A, or the scheduler:

- [Durable Execution](durable-execution.md) covers checkpoint/resume, retry, and
  circuit breaker behavior.
- [Cost Governance](cost-governance.md) covers token budgets, policy evaluation,
  downgrade chains, and usage reporting.

### Storage and Project Shape

These pages explain the current persistence baseline and the v2 slimming work
that shaped the repository:

- [Storage Migration](storage-migration.md) documents the move to SQLite as the
  embedded default and PostgreSQL for distributed deployments.
- [Project Slimming](project-slimming.md) documents removal of obsolete v1 layers
  and the shift to apcore protocol adapters.

## Reading Paths

For a new operator:

1. [Quick Start](../getting-started/quick-start.md)
2. [Task Orchestration](../guides/task-orchestration.md)
3. [REST / HTTP API](rest-api.md)
4. [Built-in Scheduler](scheduler.md)

For an integrator building executors or protocol surfaces:

1. [apcore Bridge](apcore-bridge.md)
2. [Function Executor](function-executor.md)
3. [Extension Registry](../architecture/extension-registry-design.md)
4. [REST / HTTP API](rest-api.md)

For a maintainer working on execution semantics:

1. [Task Orchestration Design](../architecture/task-orchestration.md)
2. [Durable Execution](durable-execution.md)
3. [Cost Governance](cost-governance.md)
4. [Built-in Scheduler](scheduler.md)

## Cross-Cutting Concerns

- **Task execution path:** `TaskExecutor` and `TaskManager` are shared by REST,
  MCP, A2A, CLI, scheduler, and Python callers. Execution behavior should be
  changed once in the shared path rather than reimplemented per protocol.
- **Structure tree vs execution DAG:** `parent_id` is the organization and reuse
  tree; `dependencies` is the execution-order DAG. These are intentionally
  separate concepts.
- **Durability and governance:** Checkpoint/retry/circuit breaker and
  budget/policy enforcement both attach to task execution. Changes in one area
  should be tested against the other.
- **Storage backends:** SQLite is the zero-config embedded default. PostgreSQL is
  required for distributed worker coordination.
- **Docs status:** Feature specs are implementation context, not the sole source
  of current user-facing behavior. Keep guides, architecture pages, `llms.txt`,
  and README aligned when feature specs change.
