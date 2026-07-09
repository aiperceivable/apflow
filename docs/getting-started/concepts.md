---
description: Core concepts for apflow tasks, task trees, dependencies, executors, AI-perceivable modules, and creation modes.
---

# Core Concepts

## What is apflow?

apflow is an **AI-Perceivable Distributed Orchestration** engine. It coordinates complex multi-step workflows — and every capability is automatically discoverable by AI agents through MCP/A2A/CLI.

## Tasks

A **task** is a unit of work: fetch data, process results, send notifications. Each task has:

- **id** — unique identifier
- **name** — human-readable name
- **status** — pending, in_progress, completed, failed, cancelled
- **priority** — 0 (urgent) to 3 (low)
- **inputs** — parameters for execution
- **result** — output after completion

## Task Trees (Structure)

Tasks are organized in **trees** using `parent_id`. The tree defines **who belongs to whom**:

```
Root Task (aggregates everything)
├── Step A (data source)
├── Step B (data source)
└── Step C (processing)
```

The tree structure enables powerful operations: **copy**, **link**, **archive**, and **mixed** — you can clone entire workflows, reference completed results without duplicating payload data, or freeze completed workflow rows for audit.

## Dependencies (Execution Order)

The `dependencies` field defines **execution order** — which tasks must complete before another can start:

```python
{"id": "merge", "dependencies": [{"id": "step_a"}, {"id": "step_b"}]}
# merge waits for both step_a AND step_b to complete (fan-in)
```

This is a **DAG** (Directed Acyclic Graph) — more flexible than the tree, supporting fan-in, fan-out, and cross-branch dependencies.

## The Dual Model

```
parent_id    → Structure tree (who belongs to whom)   → copy, link, archive
dependencies → Execution DAG  (who waits for whom)    → parallel, fan-in, ordering
```

Both coexist. Use `parent_id` for organization, `dependencies` for execution control. Simple workflows need only `parent_id`.

## Executors

An **executor** is the code that actually runs a task. Built-in executors include REST API calls and result aggregation. Register your own with `@function_executor`:

```python
@function_executor(id="my_task", description="Do something")
async def my_task(inputs: dict) -> dict:
    return {"result": "done"}
```

## AI-Perceivable

Every capability (create, execute, copy, cancel, etc.) is registered as an **apcore Module** and automatically exposed via:

- **MCP** — AI agents (Claude, Cursor) discover and call tools
- **A2A** — Services invoke via HTTP
- **CLI** — Humans operate from terminal

This is what makes apflow "AI-Perceivable" — the orchestration engine is transparent to AI agents.

## Five Creation Modes

| Mode | Use When |
|------|----------|
| **Create** | Building a new workflow from scratch |
| **Link** | Referencing a completed workflow without duplicating payload data |
| **Copy** | Cloning a workflow to re-run with different parameters |
| **Archive** | Freezing an existing completed workflow for audit/compliance |
| **Mixed** | Re-running only some steps, linking others |
