---
description: Guide to apflow task orchestration patterns, workflow reuse, custom executors, dependencies, and distributed execution.
---

# Task Orchestration Guide

## Basic Pipeline

```python
tasks = [
    {"id": "step1", "name": "Fetch", "priority": 1},
    {"id": "step2", "name": "Process", "parent_id": "step1", "priority": 2},
    {"id": "step3", "name": "Notify", "parent_id": "step2", "priority": 3},
]
tree = await task_creator.create_task_tree_from_array(tasks)
await task_manager.distribute_task_tree(tree)
```

## Parallel Execution

Tasks with the same priority and satisfied dependencies execute in parallel:

```python
tasks = [
    {"id": "a", "name": "Fetch A", "priority": 1},
    {"id": "b", "name": "Fetch B", "priority": 1},
    {"id": "c", "name": "Fetch C", "priority": 1},
    # a, b, c all run in parallel (same priority, no mutual deps)
]
```

## Fan-In (Multiple Sources → One Merge)

```python
tasks = [
    {"id": "src1", "name": "Source 1", "priority": 1},
    {"id": "src2", "name": "Source 2", "priority": 1},
    {"id": "merge", "name": "Merge All", "parent_id": "src1", "priority": 2,
     "dependencies": [{"id": "src1"}, {"id": "src2"}]},
]
# merge waits for BOTH src1 and src2 to complete
```

## Fan-Out (One Source → Multiple Consumers)

```python
tasks = [
    {"id": "data", "name": "Fetch Data", "priority": 1},
    {"id": "report", "name": "Generate Report", "parent_id": "data", "priority": 2,
     "dependencies": [{"id": "data"}]},
    {"id": "notify", "name": "Send Notification", "parent_id": "data", "priority": 2,
     "dependencies": [{"id": "data"}]},
]
# report and notify both depend on data, execute in parallel after data completes
```

## Reusing Workflows

### Copy (re-run with changes)

```python
original = await task_repository.get_task_by_id("completed_task_id")
new_tree = await task_creator.from_copy(original, inputs={"param": "new_value"})
await task_manager.distribute_task_tree(new_tree)
```

### Link (reference without copying)

```python
linked = await task_creator.from_link(original)
# linked.task points to original — read-only, without duplicating payload data
```

### Archive (freeze in place)

```python
archived = await task_creator.from_archive(original)
# Existing completed task rows are marked as archived for audit trail
```

### Mixed (partial re-run)

```python
mixed = await task_creator.from_mixed(
    original,
    _link_task_ids=["expensive_step"],  # Don't re-run this
    inputs={"param": "new_value"},       # Re-run others with new params
)
```

## Custom Executors

```python
from apflow.adapters.function_executor import function_executor

@function_executor(
    id="call_api",
    description="Call an external API",
    input_schema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
)
async def call_api(inputs: dict) -> dict:
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(inputs["url"])
        return {"status": resp.status_code, "data": resp.text[:1000]}
```

## Durability

Set `max_attempts` on tasks for automatic retry:

```python
{"name": "Risky Step", "max_attempts": 5, "backoff_strategy": "exponential"}
```

## Cost Governance

Set `token_budget` and `cost_policy` for AI-related tasks:

```python
{"name": "AI Analysis", "token_budget": 10000, "cost_policy": "auto-downgrade"}
```

## Distributed Mode

```bash
# All nodes run as workers; leader election is handled automatically
apflow worker --db postgresql://user:pass@host/db
apflow worker --db postgresql://user:pass@host/db --node-id worker-1
apflow worker --db postgresql://user:pass@host/db --node-id worker-2
```
