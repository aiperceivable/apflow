# Quick Start

Get running in 5 minutes.

## 1. Start a Server

```bash
# A2A HTTP server (for services and agents)
apflow serve --explorer

# Or MCP server (for Claude/Cursor)
apflow mcp
```

Open http://localhost:8000/explorer to see all available tools.

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

# Link to a completed workflow (read-only, zero storage)
link_tree = await app.task_creator.from_link(original_task)

# Archive a workflow (immutable snapshot)
archive_tree = await app.task_creator.from_archive(original_task)
```

## 5. Let AI Agents Use apflow

All task operations are automatically exposed as MCP tools:

```
apflow.task.create_tree   — Create multi-step workflow
apflow.task.execute       — Execute a task
apflow.task.copy          — Clone with modifications
apflow.task.link          — Reference completed workflow
apflow.task.cancel        — Cancel running tasks
apflow.task.tree          — View full tree structure
apflow.schedule.set       — Configure a task's schedule
... and 14 more tools
```

Start MCP server: `apflow mcp` — Claude/Cursor will discover these tools automatically.

## Next Steps

- [Core Concepts](concepts.md) — Understand the dual model (tree + DAG)
- [Task Orchestration Guide](../guides/task-orchestration.md) — Advanced patterns
- [Architecture](../architecture/task-orchestration.md) — Full design rationale
