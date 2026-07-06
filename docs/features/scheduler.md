# Feature Spec: Built-in Scheduler (F-013)

**Feature ID:** F-013
**Priority:** P1
**Phase:** Phase 4 (0.22.0)
**Tech Design Reference:** [apcore Bridge (F-002)](apcore-bridge.md), [REST / HTTP API (F-012)](rest-api.md)

---

## Overview

apflow's built-in scheduler polls the database for scheduled tasks that are due and
executes them in-process via direct database access, using asyncio for lightweight
single-node scheduling without external dependencies.

The scheduler operates as a **pull-based** loop: it wakes at a configurable interval,
fetches tasks whose `next_run_at` has passed, and executes each task tree atomically.
This complements the **push-based** path: external tools (cron, Kubernetes CronJob,
Temporal) call `POST /modules/schedule.trigger` or the `POST /webhook/trigger/{task_id}`
endpoint to start a task on demand.

**Consistency model (single-node):** one poll loop runs in one process. Duplicate
execution is prevented by the `mark_scheduled_task_running` atomic status transition
combined with an in-process active-task set. For multi-node clusters, use `apflow worker`
(PostgreSQL lease-based coordination) instead.

---

## CLI: `apflow scheduler`

Run the poll loop in the foreground until interrupted (Ctrl+C).

```bash
apflow scheduler [OPTIONS]
```

### Options

| Option | Type | Default | Description |
|---|---|---|---|
| `--poll-interval` | int | 60 | Seconds between due-task checks |
| `--max-concurrent` | int | 10 | Max tasks to execute concurrently |
| `--user-id` | string | None | Only process this user's scheduled tasks |
| `--task-timeout` | int | 3600 | Per-task execution timeout (seconds) |
| `--db` | string | None | Database connection string (overrides env/config) |
| `--verbose` | flag | False | Print each task execution result to the console |
| `--log-level` | string | None | Log level: DEBUG / INFO / WARNING / ERROR |

### Usage examples

```bash
# Check every 30 seconds, run up to 5 tasks concurrently
apflow scheduler --poll-interval 30 --max-concurrent 5

# Restrict to a single user and print results
apflow scheduler --user-id alice --verbose

# Override the database and enable debug logging
apflow scheduler --db sqlite:///./tasks.db --log-level DEBUG
```

**Database binding:** When `--db` is provided, the global session pool is initialized
against that database before the scheduler loop starts, so the direct-DB execution path
uses it end to end. Without `--db`, the pool falls back to `APFLOW_DATABASE_URL` /
the active config.

**Execution path:** The scheduler reads due tasks directly from the database (no
`APFLOW_API_SERVER_URL` required). Set `APFLOW_API_SERVER_URL` only if you want task
execution to be routed through a running REST server instead.

---

## Schedule Modules

Five modules are registered under `apflow.schedule.*` and are accessible over REST,
MCP, A2A, and the CLI. Together they form the scheduling lifecycle:

```
schedule.set → schedule.due → schedule.trigger → schedule.complete → schedule.export_ical
```

### `schedule.set`

Configure (or update) a task's schedule and compute its first next run time.

**Inputs:**

| Field | Type | Required | Description |
|---|---|---|---|
| `task_id` | string | yes | Task to configure |
| `schedule_type` | string | yes | Schedule kind, e.g. `cron` or `interval` |
| `schedule_expression` | string | yes | Cron expression or interval spec |
| `schedule_enabled` | boolean | no (default: `true`) | Enable or disable the schedule |
| `max_runs` | integer (≥1) | no | Optional cap on total run count |

**Returns:** Updated task dict (full task fields including `next_run_at`).

**Behavior:** Validates the expression against `ScheduleCalculator` before writing.
Raises `ValueError` for an invalid type/expression; raises `KeyError` if the task
does not exist.

---

### `schedule.due`

List scheduled tasks whose `next_run_at` has arrived. Designed for external schedulers
that want to poll and execute tasks themselves.

**Inputs:**

| Field | Type | Required | Description |
|---|---|---|---|
| `user_id` | string | no | Filter to this user's tasks |
| `limit` | integer (1–1000) | no (default: 100) | Maximum tasks to return |

**Returns:** `{"tasks": [...], "count": N}` — each task includes `id`, `name`,
`schedule_type`, `schedule_expression`, `next_run_at`.

**Annotations:** readonly, idempotent, paginated.

---

### `schedule.trigger`

Trigger a scheduled task to execute now. This is the registry-native form of the
inbound webhook: external schedulers (cron, Kubernetes CronJob, Temporal) call it
to push-trigger a task, complementing the built-in poll loop's pull model. Internally
reuses `WebhookGateway`, which marks the task running, executes its tree, and advances
the next run time.

**Inputs:**

| Field | Type | Required | Description |
|---|---|---|---|
| `task_id` | string | yes | Task ID to trigger |
| `user_id` | string | no | Optional owner check; rejects tasks owned by others |
| `async_execution` | boolean | no (default: `false`) | Return immediately and run in the background |

**Returns (synchronous):** `{"success": bool, "status": str, "task_id": str, "result": ..., "error": str|null, "children": [...]}`.

**Returns (async):** `{"success": true, "status": "triggered", "task_id": str, "message": "..."}`.

**Behavior:** Raises `KeyError` if the task does not exist (REST maps this to 404).
Synchronous mode (the default) waits for the full task tree to complete before returning.
Async mode fires the execution as a background task and returns immediately.

---

### `schedule.complete`

Record a scheduled run as complete and advance the next run time. Used by external
schedulers to report back after executing a task themselves (pull-execute model).

**Inputs:**

| Field | Type | Required | Description |
|---|---|---|---|
| `task_id` | string | yes | Task ID |
| `success` | boolean | no (default: `true`) | Whether the run succeeded |
| `error` | string | no | Error message if the run failed |
| `calculate_next_run` | boolean | no (default: `true`) | Recompute next run time |

**Returns:** Updated task dict.

**Behavior:** Raises `KeyError` if the task does not exist. Raises `RuntimeError` if
the task exists but the schedule completion fails (e.g. corrupt schedule configuration).

---

### `schedule.export_ical`

Export scheduled tasks as an iCalendar (`.ics`) feed for display in calendar
applications.

**Inputs:**

| Field | Type | Required | Description |
|---|---|---|---|
| `user_id` | string | no | Filter to this user's tasks |
| `schedule_type` | string | no | Filter by schedule type |
| `enabled_only` | boolean | no (default: `true`) | Include only enabled schedules |
| `limit` | integer (1–1000) | no (default: 100) | Max tasks to include |
| `base_url` | string | no | Base URL for task deep-links in the feed |

**Returns:** `{"ical": "<iCal text>", "format": "text/calendar"}`.

**Annotations:** readonly, idempotent.

---

## Run History (Clone-per-fire)

The scheduler uses a **clone-per-fire** model: instead of executing the scheduled task in
place (which would overwrite the previous `result` and leave no history), each fire
**clones the definition into a fresh run instance and executes that instance**. The
definition itself never executes — it only advances its schedule. The set of run
instances *is* the history, including each fire's `token_usage`.

This is not just for history: it is the model the distributed execution stack requires.
Lease, idempotency, and checkpoint all key on `task_id` assuming a task is executed once,
so a recurring in-place task would break them. A fresh clone per fire is a normal
one-shot task the distributed machinery already handles — see
[Task Orchestration › System-generated Origin](../architecture/task-orchestration.md#system-generated-origin-scheduled_run).

### Design

On each fire (both the poll loop and the `schedule.trigger` push path):

1. `TaskCreator.instantiate_scheduled_run(definition)` clones the definition's tree into a
   fresh **pending** run stamped `origin_type=scheduled_run`, with all `schedule_*` fields
   cleared (`schedule_enabled=false` → never re-selected by `schedule.due`) and
   `original_task_id` pointing back to the definition.
2. The scheduler executes the **run instance** (single-node: in-process; distributed:
   the leader leaves it `pending` for a worker to lease — same model, different execution
   venue; see [Distributed Scheduling](#distributed-scheduling-cluster)).
3. `complete_scheduled_run(definition)` advances the definition's `run_count` and
   `next_run_at`. The definition's own execution fields are never touched.

Reuses the existing Copy/Mixed clone engine (`_clone_task_tree`) — no new table, and no
`is_template` / `template_id` columns.

### Querying history

```sql
-- Every run of a scheduled definition, newest first
SELECT * FROM apflow_tasks
WHERE original_task_id = :definition_id AND origin_type = 'scheduled_run'
ORDER BY created_at DESC;
```

`TaskRepository.list_scheduled_runs(task_id, limit, offset)` wraps this query. Each row
carries that fire's `result`, `status`, `error`, and `token_usage`.

**Storage note:** history accumulates one run-tree per fire. High-frequency schedules
should apply a retention policy (prune by age or keep the most recent N) rather than
retaining every run indefinitely.

---

## Distributed Scheduling (cluster)

In a multi-node cluster, scheduling and execution are decoupled:

- **The elected leader** runs a *dispatch-only* scheduler. On each due schedule it
  instantiates a fresh pending run instance (`instantiate_scheduled_run`) and advances the
  definition's schedule — but does **not** execute it. Because leadership is single-holder
  (leader election), exactly one node dispatches, so a schedule never fires on every node.
- **Workers** across the cluster lease the pending run instances (`status=pending`) and
  execute them, using the existing lease / idempotency / checkpoint machinery. Each run is
  a distinct `task_id`, so those primitives work unchanged — the reason clone-per-fire is
  the distributed-native model.

```bash
# Each node: a worker that also participates in leader election; the leader dispatches.
apflow worker --db postgresql://… --scheduler
```

Configuration (env or `DistributedConfig`):

| Setting | Env | Default | Description |
|---|---|---|---|
| `scheduling_enabled` | `APFLOW_CLUSTER_SCHEDULING` | `false` | Leader runs the dispatch scheduler |
| `scheduler_poll_interval_seconds` | `APFLOW_SCHEDULER_POLL_INTERVAL` | `30` | Seconds between due-schedule checks |

On leader change (renewal failure → demotion, or graceful shutdown) the dispatch scheduler
is stopped, so the new leader becomes the sole dispatcher. Distributed mode requires a
PostgreSQL `--db` (the coordination primitives need its atomic guarantees).

**Boundary:** distributed execution of a run *tree* (children, cross-node dependency
ordering) is the worker runtime's existing responsibility; the scheduler only produces the
pending run instances. Duplicate-fire safety across the cluster comes from single-leader
dispatch; a compare-and-swap claim on `next_run_at` is a possible future hardening.

---

## Integration with REST / Unified Server

The built-in scheduler can be embedded in the REST or unified server process so it
shares the same event loop (and SQLite single writer), removing the need for a
separate scheduler process.

```bash
# REST server with embedded poll loop
apflow rest --scheduler

# Unified REST + A2A + MCP server with embedded poll loop
apflow serve --all --scheduler
```

`--scheduler` requires `--all` when used with `apflow serve`: the A2A-only path runs
`apcore-a2a`'s blocking serve, which provides no event-loop hook for the poll loop.
When that combination is not available, run `apflow scheduler` as a standalone process.

### Inbound webhook (optional)

An HTTP webhook endpoint for external push triggers is mounted separately:

```bash
apflow rest --webhook --webhook-secret <hmac-secret>
apflow serve --all --webhook --webhook-secret <hmac-secret>
```

The webhook path (`POST /webhook/trigger/{task_id}`) is JWT-exempt and carries its
own HMAC-SHA256 credential. Under `--auth`, providing `--webhook-secret` is required
— omitting it would leave the endpoint unauthenticated while `--auth` protects the
rest of the surface.

---

## Example Workflow

```bash
# Terminal 1: start the REST server with an embedded scheduler
apflow rest --scheduler --verbose

# Terminal 2: create a task (must exist before scheduling it)
curl -s -X POST http://localhost:8080/modules/task.create \
  -H "Content-Type: application/json" \
  -d '{"name": "daily-report"}' | jq .id
# -> "abc-123"

# Configure the task to run every day at 09:00
curl -s -X POST http://localhost:8080/modules/schedule.set \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "abc-123",
    "schedule_type": "cron",
    "schedule_expression": "0 9 * * *"
  }' | jq .next_run_at

# Trigger the task immediately (push mode, without waiting for the poll loop)
curl -s -X POST http://localhost:8080/modules/schedule.trigger \
  -H "Content-Type: application/json" \
  -d '{"task_id": "abc-123"}' | jq .

# Export schedules as iCal for a calendar application
curl -s -X POST http://localhost:8080/modules/schedule.export_ical \
  -H "Content-Type: application/json" \
  -d '{"base_url": "http://localhost:8080"}' | jq -r .ical
```

The poll loop (embedded via `--scheduler`) and the push trigger (`schedule.trigger`)
are independent execution paths for the same task. Use the poll loop for automatic
periodic execution; use the push trigger for on-demand or externally-controlled runs.
