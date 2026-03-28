# apflow v2 Feature Specifications -- Overview

**Version:** 0.20.0 (MVP)
**Date:** 2026-03-28
**Tech Design:** `docs/apflow-v2/tech-design.md`
**PRD:** `docs/apflow-v2/prd.md`

---

## Feature Index

| ID | Feature | Spec File | Priority | Phase |
|---|---|---|---|---|
| F-SM | Storage Migration (DuckDB to SQLite) | `storage-migration.md` | P0 | Phase 1 |
| F-001 | Project Slimming | `project-slimming.md` | P0 | Phase 1 |
| F-002 | apcore Module Bridge | `apcore-bridge.md` | P0 | Phase 1 |
| F-003 | Durable Execution | `durable-execution.md` | P0 | Phase 2 |
| F-004 | Cost Governance | `cost-governance.md` | P0 | Phase 3 |
| F-005 | TaskCreator Relaxation | `task-creator-relaxation.md` | P0 | Phase 1 |

## Dependency Graph

```
F-SM (Storage Migration)
  |
  v
F-001 (Project Slimming) --- depends on F-SM (pyproject.toml changes overlap)
  |
  v
F-005 (TaskCreator Relaxation) --- independent, can run in parallel with F-002
  |
F-002 (apcore Module Bridge) --- depends on F-001 (deleted modules must be gone)
  |
  v
F-003 (Durable Execution) --- depends on F-SM (migration 004 needs SQLite support)
  |
  v
F-004 (Cost Governance) --- depends on F-003 (shares migration 004, TaskModel fields)
```

## Implementation Order

1. **F-SM + F-001 + F-005** (Week 1-2): Storage migration, delete dead code, relax TaskCreator. These are independent changes that can be done in parallel branches and merged sequentially.
2. **F-002** (Week 2-3): Build the bridge after slimming is complete, so no deleted module references leak.
3. **F-003** (Week 4-6): Durable execution builds on the new SQLite dialect and migration infrastructure.
4. **F-004** (Week 7-9): Cost governance shares the migration 004 with F-003 and integrates into the same TaskManager hooks.

## Cross-Cutting Concerns

- **Migration 004** serves both F-003 (durability fields, task_checkpoints table) and F-004 (cost governance fields). It is a single migration file.
- **TaskManager integration** is modified by both F-003 (retry/checkpoint in `_execute_single_task`) and F-004 (budget check/update in `_handle_task_execution_result`). These touch different methods and do not conflict.
- **TaskModel fields** are added by both F-003 and F-004. They are independent columns with no interaction.
- **pyproject.toml** is modified by F-SM (remove duckdb-engine) and F-001 (remove deleted extras, add apcore). These changes merge cleanly.
