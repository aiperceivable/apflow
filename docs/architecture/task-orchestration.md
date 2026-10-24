---
description: Architecture of apflow task orchestration, including structure trees, execution DAGs, reuse modes, and scheduling history.
---

# Task Orchestration Architecture

## The Dual Model: Structure Tree + Execution DAG

apflow uses two independent, complementary mechanisms to organize and execute tasks. This is not a compromise — it is the correct design for an orchestration engine that supports creation, execution, reuse, and archival of workflows.

```
parent_id    → Structure Tree (who belongs to whom)
dependencies → Execution DAG  (who waits for whom)
```

### Why Two Models?

Think of it like a car:

```
Physical structure:  Engine parts belong to the engine assembly,
                     which belongs to the powertrain system.

Electrical wiring:   Battery powers the starter motor,
                     ECU controls the fuel injectors.

You don't throw away the assembly diagram just because
you have a wiring diagram. They serve different purposes.
```

In apflow:

| Mechanism | Field | Purpose | Supports |
|-----------|-------|---------|----------|
| **Structure Tree** | `parent_id` | Organizational hierarchy — which tasks form a group | Create, Copy, Link, Archive, Mixed, Progress aggregation |
| **Execution DAG** | `dependencies` | Execution ordering — which tasks must complete first | Parallel execution, Fan-in, Fan-out, Result injection |

### The Operation Matrix

```
                Create   Execute   Copy   Link   Archive   Mixed
Structure Tree    ✅       -        ✅     ✅      ✅       ✅
Execution DAG     -        ✅       -      -       -        -
```

The structure tree supports 5 core operations. The execution DAG supports 1. Removing the tree to make the DAG "purer" would lose 5 of 6 capabilities.

---

## Task Lifecycle: Five Creation Modes

### 1. Create — Build from scratch

```python
tasks = [
    {"id": "fetch", "name": "Fetch Data", "priority": 1},
    {"id": "process", "name": "Process", "parent_id": "fetch", "priority": 2},
]
tree = await task_creator.create_task_tree_from_array(tasks)
```

Validates uniqueness, resolves name→ID references, detects circular dependencies, persists to database, builds in-memory tree.

### 2. Link — Reference a completed workflow (read-only pointer)

```python
tree = await task_creator.from_link(original_task)
```

- Creates new tasks that **point to** the originals via `original_task_id`
- Result/params/inputs/schemas are cleared (the link reads from the original)
- Requires entire source tree to be `completed`
- Does not duplicate data — minimal storage cost
- Source and Link must have the same owner; linking does not grant cross-user access.
- Tree queries and dependency injection share bounded effective-result resolution.
  Missing originals, ownership violations, and reference cycles raise explicit errors;
  a legitimate `null` result remains valid. Each consumer receives an isolated copy.

**Use case:** Multiple workflows owned by the same user reference one completed analysis.

### 3. Copy — Clone a workflow (modifiable)

```python
tree = await task_creator.from_copy(original_task, priority=0, inputs={"url": "new_url"})
```

- Creates a full copy with new UUIDs
- All `dependencies` IDs are automatically remapped to new IDs
- Upstream dependencies are auto-included (`_auto_include_deps=True`)
- Override any field via `**reset_kwargs`

**Use case:** "Run yesterday's pipeline again with different parameters."

### 4. Archive — Freeze an existing workflow in place

```python
tree = await task_creator.from_archive(original_task)
```

- Marks existing tasks as `origin_type=archive` (frozen, read-only)
- Preserves all data including results and keeps the original task IDs
- Requires entire tree to be `completed`

**Use case:** Audit trail and compliance records for completed production runs.

### 5. Mixed — Partial copy + partial link

```python
tree = await task_creator.from_mixed(
    original_task,
    _link_task_ids=["expensive_step_1", "expensive_step_3"],
    inputs={"param": "new_value"},
)
```

- Tasks in `_link_task_ids` are linked (referenced, not copied)
- All other tasks are copied (modifiable)
- Dependency remapping handles both linked and copied IDs

**Use case:** Re-run a 10-step pipeline but skip the expensive steps that haven't changed — link to their existing results, copy and re-execute only the changed steps.

---

## System-generated Origin: `scheduled_run`

The five modes above are **user-initiated**. There is one additional `TaskOriginType`
value that only the **scheduler** produces: `scheduled_run`.

The scheduler follows a **clone-per-fire** model. Each time a scheduled task fires, the
**definition is cloned into a fresh run instance** and that instance executes — the
definition itself never runs. This matches how distributed schedulers work (Argo creates
a Workflow per cron hit, Kubernetes a Job per CronJob hit) and, critically, matches the
distributed execution stack's assumption that a `task_id` is executed **once** (lease,
idempotency, and checkpoint all key on `task_id`). A recurring in-place task would violate
that assumption; a fresh clone per fire honors it.

```python
# Emitted internally by the scheduler on each fire — not a public creation mode.
run = await task_creator.instantiate_scheduled_run(definition)   # fresh pending clone
# ... the scheduler then executes `run`, not `definition` ...
```

- Reuses `_clone_task_tree` (the same engine behind Copy/Mixed) — no new clone logic.
- Stamps `origin_type=scheduled_run` to distinguish scheduler runs from user copies.
- Resets execution state to a fresh **pending** run (`status`, `result`, `error`,
  `progress`, `token_usage` cleared) and **clears** all `schedule_*` fields, `run_count`,
  `max_runs`, so the run instance is never itself picked up by the poll loop.
- Points back to the definition via `original_task_id`, giving a queryable history.

**Single-node vs distributed — one model.** The single-node scheduler executes the clone
in-process. In a cluster, the elected leader runs a *dispatch-only* scheduler
(`apflow worker --scheduler`): it instantiates the same pending clone but leaves it for a
worker to lease and execute. Only the execution venue differs — the clone-per-fire model
is identical, so there is no model change at the distributed boundary.

### Why no `is_template` / `template_id` columns

Supporting run history needs neither a template flag nor a template foreign key:

| Concern | Handled by (existing) |
|---|---|
| "This row is the recurring definition, poll it" | `schedule_enabled=true` (the poll query already filters on it) |
| "This row is a run instance, do not re-fire it" | the instance has `schedule_enabled=false` → auto-excluded from polling |
| "Which definition did this run come from?" | `original_task_id` (populated by the clone engine) |
| "Distinguish scheduler runs from manual copies" | `origin_type=scheduled_run` |

**History query:** `WHERE original_task_id=<definition> AND origin_type='scheduled_run'`.

**Edge case:** the clone engine sets `original_task_id` to the *most original* ancestor.
When the scheduled definition was itself created fresh (`origin_type=create`, the common
case) this is exactly the definition; if the definition is itself a copy, runs group under
that ancestor instead. Promote to a dedicated `template_id` column only if precise
provenance for copied-then-scheduled definitions is required.

---

## Execution Model

### Priority + Dependency Hybrid

TaskManager uses **both** priority and dependencies to determine execution order:

```
1. Group children by priority (0=urgent → 3=low)
2. For each priority group:
   a. Check each task's dependencies → separate into ready vs waiting
   b. Ready tasks → asyncio.gather() (parallel execution)
   c. Waiting tasks → deferred (triggered later by cascade)
3. After all children complete → execute parent if its dependencies are satisfied
4. Cascade: when any task completes, scan for newly-ready waiting tasks
```

### Execution Order: Roots First, Fruit Last (Like a Real Tree)

apflow's task tree follows the natural growth of a plant — not an organizational chart:

```
  🍎 Fruit (root task)     ← Last to ripen: aggregates results
      |
  🌿 Branches (middle)    ← Process and transform
      |
  🌱 Roots (leaf tasks)   ← First to execute: absorb data from sources
```

```
      A (priority=2)       ← Fruit: aggregates B and C's results
     / \
    B   C (priority=1)     ← Roots: execute first, gather raw data
```

This is the **botanical model** of a tree: roots absorb nutrients first → nutrients flow upward through the trunk → fruit ripens last. In apflow: leaf tasks (data fetching) execute first → results propagate upward → the root task (aggregation/output) executes last.

The tree also serves as an **organizational model**: `parent_id` defines which tasks belong together as a group — enabling copy, link, archive, and mixed operations on entire subtrees.

```
Botanical aspect:     Execution direction (leaves → root)
Organizational aspect: Structural grouping (who belongs to whom)
Execution ordering:    Controlled by DAG (dependencies), not by the tree
```

### DAG Patterns

**Fan-out (one triggers many):**
```python
{"id": "A", "name": "Root"},
{"id": "B", "parent_id": "A", "priority": 1},  # B and C
{"id": "C", "parent_id": "A", "priority": 1},  # execute in parallel
```

**Fan-in (many feed one):**
```python
{"id": "B", "name": "Step B", "priority": 1},
{"id": "C", "name": "Step C", "priority": 1},
{"id": "D", "name": "Merge", "parent_id": "B", "priority": 2,
 "dependencies": [{"id": "B"}, {"id": "C"}]},  # D waits for both
```

**Cross-tree dependencies:**
```python
{"id": "A", "name": "Tree 1 Root"},
{"id": "B", "name": "Tree 2 Root"},
{"id": "C", "parent_id": "A",
 "dependencies": [{"id": "B"}]},  # C is in Tree 1, depends on Tree 2's B
```

### Executor Result Protocol

New executors declare `result_format = "envelope"` and return an execution envelope:

```python
{
    "result": {"answer": 42, "evidence": ["document-1"]},
    "token_usage": {"input": 100, "output": 20, "total": 120},
}
```

`result` contains the complete business output. `error` is an optional non-empty
failure message (or `null`); a failure may include partial business output.
`token_usage` is an optional object with non-negative integer `input`, `output`,
and `total` counters; omitted `total` is calculated from input and output.
Other outer keys are rejected: put business evidence and other outputs inside
`result`. A successful envelope must include `result`, including when it is `null`.
A failure may omit `result`; both runtime normalization and generated schemas
accept that form. When a business output schema is declared, successful results
must satisfy it, so successful `null` requires an explicitly nullable schema.
Failures may use `null` instead of a business value or provide partial output
that satisfies the business schema.

TaskManager interprets the declared outer envelope once, stores its business
payload in `TaskModel.result`, records the error separately, and accumulates usage
in `TaskModel.token_usage`, even without budget enforcement. Failed executions
and explicit cancellation responses can also report usage. Exceptions without
usage metadata are not assigned estimated consumption. Returned failure envelopes
are not automatically retried; the existing exception retry policy is unchanged.

`get_output_schema()` describes the actual return from `execute()`. Envelope
schemas contain the business schema inside `properties.result`. Built-in
executors and generated function executors return envelopes; direct bridge calls
also return and validate those envelopes. Explicit raw executor classes are
wrapped by the bridge too, with a matching generated schema, because apcore
module outputs must be objects even when business output is a scalar or null.
Legacy bridge calls retain their original public return shape. The root execution API retains its
`status`, `progress`, and `root_task_id` summary, with only the root business
payload in its `result`, including `null`. Task retrieval returns the stored
business payload and execution metadata in separate fields.

Formats are explicit, never inferred from the presence of a business key:

| `result_format` | Actual `execute()` return | Framework interpretation |
|---|---|---|
| `envelope` | `{result, error?, token_usage?}` | Extract one outer business result and process metadata |
| `raw` | Any JSON business value | Preserve everything; business `error`, `result`, and `token_usage` have no special meaning |
| `legacy` (default for existing executor classes) | Existing return value | Preserve the entire value while retaining historical top-level error/usage recognition |

`@function_executor` treats the function's return as `raw` business output by
default and wraps it once. Its `result_format` argument declares the underlying
function's format; the generated executor always declares `envelope`. For raw
functions, `output_schema` / `output_model` describes their business output and
the adapter generates the outer schema. With `result_format="envelope"`, those
parameters describe the complete function envelope instead.
For legacy functions, the complete return value is preserved inside `result`;
only `input`, `output`, and `total` are promoted from legacy `token_usage` into
the generated envelope's execution metadata. Provider-specific counters remain
available in the business payload.

Executor-specific pre-hook shortcuts follow that executor's declared format;
executor post-hooks see the original executor return. Task post-hooks see stored
business output. `get_demo_result()` supplies a business example independently of
`result_format`; `_demo_sleep` is consumed before wrapping, and dictionary demos
receive a business `demo_mode` marker. Cancellation uses its separate control
response (`status`, `result` or `partial_result`, `token_usage`), preserves empty
partial results, and stores usage separately. A late normal return cannot
replace an already cancelled task or count the cancellation usage again.
Cancellation counters use the same total derivation as normal envelopes; invalid
usage is logged and does not prevent cancellation. Custom envelope bridge calls
are validated without adding omitted fields that their declared schema forbids.

### Dependency Result Injection

Results flow through `dependencies`, which may connect parents, siblings, or
other consumers. `parent_id` alone does not inject results.

```
B completes → stored result: {"price": 9.99}
C completes → stored result: {"price": 19.99}
D starts    → inputs: {"B_id": {"price": 9.99}, "C_id": {"price": 19.99}}
```

Each dependency supplies its stored business output under `inputs[dependency_id]`.
Schema field names do not establish data bindings: request configuration such as
`url` remains separate from an upstream response containing another `url`.
Consumers explicitly select business fields from their dependencies. Aggregation
collects these business outputs, without adding execution envelopes or usage.
A business field named `result` inside stored output is never unwrapped again.

Dependency ID keys are reserved for injected data and refreshed on every
resolution. Values are deep-copied. Empty objects, lists, strings, zero, `false`,
and `null` are valid completed outputs. A required dependency must be completed;
business input requirements belong to the consumer's validation. An optional
unavailable dependency removes a stale injected value. Copy, Mixed, and scheduled
cloning clear keys for the original dependency IDs before new IDs are injected.
Consumers must use their current dependency IDs rather than hard-code IDs from
an earlier copy.

**Migration:** Existing executor classes remain in explicit `legacy` mode until
they declare another format. Custom subclasses overriding a built-in executor
inherit its new `envelope` contract: update the override or explicitly declare
`legacy` / `raw`. Declare `envelope` when returning the standard wrapper; declare `raw` when every returned key belongs to business data. Update
direct calls to built-in/generated executors to read their outer `result`.
Consumers previously using automatic flat schema mapping must read the dependency
ID namespace and select the fields they need.

Existing database rows are not guessed or rewritten. Their stored result remains
opaque, even if it has a field named `result`. Re-execute with the declared new
format, or explicitly convert a known historical envelope with
`repository.update_task(task_id, result=known_envelope["result"])`, preserving its
usage/error metadata separately. Only convert rows whose origin and format are
known; do not apply this operation to arbitrary stored business dictionaries.

### Cascade Triggering

After any task completes, `execute_after_task()` scans all pending tasks in the same tree. If any task's dependencies are now satisfied, it executes immediately. This handles complex DAG patterns where tasks become ready only after multiple predecessors complete.

---

## Validation Pipeline

All validation happens at **creation time**, not execution time:

| Check | When | Method |
|-------|------|--------|
| Name uniqueness | Create | `task_dicts_to_task_models()` |
| ID uniqueness | Create | `task_dicts_to_task_models()` |
| Name→ID resolution | Create | Automatic mapping |
| Circular dependency detection | Create | DFS in `detect_circular_dependencies()` |
| Reference validity | Create | All parent_id and dependency IDs must exist in array |
| DB existence check | Create | Non-UUID IDs checked against database |
| External dependency validation | Copy/Link | `_validate_no_external_dependencies()` |

---

## Two Analogies

### The Plant Analogy (Execution Model)

```
🍎 Fruit (root task)      = Final output, aggregated result
🌿 Branches               = Intermediate processing
🌱 Roots (leaf tasks)     = Data sources, first to execute

Nutrients flow upward: roots absorb → trunk transports → fruit ripens.
Results flow upward:   leaf tasks execute → results propagate → root aggregates.
```

apflow's tree is a **botanical tree**, not an org chart. The "root" task in the data model is the **fruit** in the botanical model — it ripens last because it depends on everything below it.

### The Tesla Analogy (System Design)

```
Tesla's physical assembly   = apflow's structure tree (parent_id)
  Which parts belong to which system, how to copy/replace a module.

Tesla's CAN bus wiring      = apflow's execution DAG (dependencies)
  Signal flow, execution timing, which systems communicate.

Tesla's FSD                 = AI agents (Claude, Gemini, any)
  The intelligence that perceives and controls the car.

Tesla's CAN protocol        = apcore (Module Standard)
  Makes every system perceivable and controllable by FSD.
```

apflow doesn't build FSD. It builds the best car that FSD can drive.
