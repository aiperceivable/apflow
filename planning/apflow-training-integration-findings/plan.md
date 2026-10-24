# Training Integration Findings Implementation

## Goal
Resolve all fourteen reviewed findings in the generic apflow service while preserving deterministic orchestration and existing supported library behavior.

## Architecture Design
REST/CLI/MCP/A2A enter the existing registry and per-request session/access boundary. TaskCreator and TaskRepository own transactional creation and immutable/effective reference invariants. TaskExecutor owns actual execution, cancellation, and durable dispatch; TaskManager executes each attempt through existing retry, result, and budget facilities. Network destinations and credentials come from trusted deployment policy. Training/world retain pedagogy, quality criteria, external-effect idempotency and final adoption.

## Reuse Report
- EXTEND TaskRepository update/claim methods, existing Link API resolver, and TaskCreator archive/copy methods; do not create a second repository or snapshot subsystem.
- REUSE TaskExecutor singleton registry, pooled sessions, TaskTracker, existing scheduler and task leasing. EXTEND cancellation and detached dispatch there; do not introduce a second orchestration engine.
- EXTEND existing network_security checks, RestExecutor redirect handling, registry discovery, JWT bridge context and CLI bootstrap. Retain optional plugin tolerance.
- EXTEND RetryManager, canonical result_protocol, BudgetManager and atomic usage accumulation; do not duplicate retry or accounting logic.
- EXISTING_CONFIG: deployment environment/configuration, apcore registry metadata, SQLAlchemy storage/migrations and task scope fields are authoritative.
- EXISTING_TEST_SCAFFOLDING: pytest sync/async SQLite fixtures, MockTransport, function executors and API/CLI clients. Use synthetic payloads and no external model calls.
- DELETION_CANDIDATES: none established. Remove only superseded internal branches discovered during implementation.
- NEW_CODE_BUDGET: seven task files plus existing-module edits; new runtime files justified only for a cohesive boundary with multiple concrete callers (access policy, execution dispatch/credential configuration), never speculative frameworks.
- ANTI_DUPLICATION_CALLOUT: retain original archive IDs; Link keys remain Link IDs; avoid new state enum for requested cancellation; existing completed payload can be null; existing direct execution and legacy calls stay supported.

## Design Decisions
### High-Impact Decision Record: Persistence, API, execution state, and cross-service boundaries
Necessity: persisted submission/dispatch receipts distinguish durable acceptance from waiting, cancellation request from stopping, and shared reservations from per-task usage. Each addition maps to a concrete APF acceptance condition.

Data characteristics: task writes occur per transition/attempt; receipts are keyed by owner/namespace/key and dispatch claims by status/lease. Actual production row counts and growth are deployment-specific and unknown; tests exercise indexed atomic operations rather than assuming a scale or service SLO.

Volatility: additive nullable columns and explicitly versioned structured errors retain legacy request/result fields; scope/reservation records use fixed task/attempt identities. No teaching-specific schema is introduced.

Migration cost: additive SQLAlchemy migration with nullable/default-compatible fields and unique keys; existing task rows need no business-data backfill. SQLite table recreation/DDL locks and PostgreSQL DDL require operators to run migrations before readiness; downgrade must reject incompatible retained records instead of discarding receipts silently.

External contract impact: preserve synchronous execute and existing task statuses. Detached submit/status and requested/confirmed cancellation flags are additive; known broken archive writes and cross-owner access become explicit errors. Consumers that relied on these broken behaviors must copy archives or use authorized identities.

Cheaper alternative: process-local asyncio tasks, global private-URL bypass, UI-only archive locks and preflight budget checks cannot satisfy persistence/concurrency/authorization acceptance. Reuse existing execution, scheduler, registry and storage instead of adding a queue vendor or new orchestration layer.

## Task Breakdown
```mermaid
graph TD
 core-invariants --> durable-dispatch
 bootstrap-identity --> idempotent-submission
 bootstrap-identity --> durable-dispatch
 idempotent-submission --> durable-dispatch
 core-invariants --> retry-errors
 core-invariants --> scope-budget
 retry-errors --> scope-budget
```

- `core-invariants`: Reference results, archives, and actual cancellation; 8 hours; depends on none.
- `bootstrap-identity`: Bootstrap, identity boundaries, and dependency contract; 8 hours; depends on none.
- `outbound-readiness`: Target policy, credential references, and readiness; 8 hours; depends on none.
- `idempotent-submission`: Atomic idempotent task submission; 6 hours; depends on bootstrap-identity.
- `durable-dispatch`: Persistent detached dispatch and recovery; 10 hours; depends on core-invariants, bootstrap-identity, idempotent-submission.
- `retry-errors`: Retry classification and structured errors; 6 hours; depends on core-invariants.
- `scope-budget`: Scope reservations and failed usage accounting; 8 hours; depends on core-invariants, retry-errors.

## Risks and Considerations
Cancellation of remote side effects is cooperative and does not guarantee rollback; report pending reconciliation. Worker lease expiry does not establish exactly-once external effects. Unknown cost remains unknown and blocks unreserved spending until reconciled. Outbound policies must validate redirect/connection destinations; do not claim network-isolation guarantees beyond the transport. Compatibility is verified against declared apcore versions; preserve structured upstream validation fields. Migrations and auth configuration are deployment prerequisites.

## Acceptance Criteria
- [x] All APF IDs mapped to named regression tests and passing targeted checks.
- [x] Concurrent creation, transitions, reservations and duplicate dispatch claims remain atomic.
- [x] Same-owner reference results, legal nulls and independent consumer values preserved.
- [x] Archive original IDs preserved and protected content cannot be changed or executed.
- [x] Actual cancellation notification and stale attempt guards tested through independent sessions.
- [x] Authenticated REST boundary, explicit storage CLI bootstrap and required readiness tested.
- [x] Full unit suite, ruff, Black and Pyright inspected; real-service limitations explicitly stated.
- [x] Relevant existing documentation updated with actual contracts and migration/deployment requirements.

## References
- [Source findings](/Users/tercelyi/Workspace/corefirst/corefirst-world/docs/apflow-training-integration-findings.md)
- [Task orchestration](../../docs/architecture/task-orchestration.md)
- [Durability](../../docs/features/durable-execution.md)
- [Cost governance](../../docs/features/cost-governance.md)


## Implementation Evidence

| Finding | Implemented contract | Named regression evidence |
|---|---|---|
| APF-01 | Trusted target profiles constrain origin, port, method, path and redirects; private URLs remain denied by default | `tests/test_service_connections.py::test_profile_allows_only_its_private_target` |
| APF-02 | Shared effective Link resolution preserves Link IDs, nulls and isolated consumer values | `tests/core/execution/test_training_invariants.py::test_link_dependency_matches_query_without_shared_payload` |
| APF-03 | Original-ID Archive freezes content and topology; Link/Mixed snapshots materialize effective data | `test_archive_blocks_supported_updates`, `test_mixed_archive_preserves_link_ids_dependencies_and_snapshot`, `test_archive_serializes_concurrent_child_insert` |
| APF-04 | Persisted acceptance, bounded background dispatch, root leases, full-tree resume and explicit uncertain-outcome recovery | `tests/bridge/test_dispatch.py`, `tests/api/test_service_execution_contract.py` |
| APF-05 | Public cancellation reaches the live executor; durable intent, cooperative acknowledgement and fenced late writes | `test_rest_background_tree_survives_submit_and_receives_independent_cancel`, `test_requested_cancel_completion_confirms_without_late_result` |
| APF-06 | Owner/namespace-scoped atomic creation receipts, conflicts, lookup and racing-request deduplication | `tests/bridge/test_submission.py::test_concurrent_create_commits_one_tree_and_one_receipt` |
| APF-07 | Verified JWT identity reaches JSON/SSE; ownership/reference checks and native ACL apply to actual inner arguments | `tests/bridge/test_service_boundaries.py`, `tests/bridge/test_service_policy.py` |
| APF-08 | Service-owned credential references, execution-only injection, inline credential rejection and public redaction | `tests/test_service_connections.py` |
| APF-09 | Typed/envelope failure classification, safe HTTP replay policy, bounded Retry-After and cancellable backoff | `tests/durability/test_retry_classification.py`, `tests/extensions/http/test_rest_error_info.py` |
| APF-10 | Versioned error_info, trace correlation, per-attempt usage/history and pre-executor failure phases | `tests/durability/test_attempt_history.py`, `tests/durability/test_failure_phases.py`, `test_rest_preserves_inbound_trace_and_verified_identity` |
| APF-11 | Configured required capabilities/schema versions gate startup, readiness and new admission | `tests/bridge/test_required_modules.py` |
| APF-12 | Atomic owner-scoped reservations, retry settlement, unknown liabilities and immutable reconciliation evidence | `tests/governance/test_reservations.py`, `tests/governance/test_shared_scope_execution.py`, `test_budget_admin_reconciliation_is_scoped_and_audited` |
| APF-13 | Lazy CLI metadata and application-bound request/worker/scheduler sessions preserve the selected engine | `tests/cli/test_storage_binding.py`, `tests/cli/test_cli_bootstrap.py` |
| APF-14 | Exact compatible dependency environment and lock-constrained CI retain the upstream validation schema | `tests/integration/test_package_acceptance.py`, `tests/integration/test_sdk_compatibility.py`, `tests/api/test_rest_adapter.py` |

### Operational Boundaries

- PostgreSQL SQL paths are implemented but this verification uses SQLite; no live PostgreSQL cluster, world instance, or model/provider billing calls were exercised.
- Remote effects with unknown delivery require receipt-based operator reconciliation. Cancellation does not roll back an already delivered operation.
- Hard process death can leave reservations held as `reserved`; these still consume capacity until reconciled. Strict token admission relies on trusted executor-enforced bounds and cannot promise a provider-independent monetary ceiling.
- Configure JWT identity, roles/ACL, required capabilities, target profiles and credential environment variables before deployment. Connection-time network isolation, including DNS changes, belongs to deployment egress policy.
- Migration 006 is additive. Existing corrupt historical archives are retained rather than guessed or rewritten. Downgrade refuses to discard submission and financial audit evidence.


## Final Verification

This section records the initial implementation, before the approved Run revision.

Verified on Python 3.12.11 with the isolated, exact compatible dependencies from
`uv.lock` (apcore 0.32.2, toolkit 0.14.0, MCP 0.23.1, A2A 0.8.4, CLI 0.12.5).
The original upstream keyword validation assertions remain intact.

- `PYTHONPATH=src /private/tmp/apflow-findings-deps/bin/python -m pytest -q`: **1761 passed, 10 skipped, 34 warnings**, 195.02 seconds. Default configuration excludes manual/slow tests and two separately checked storage files.
- Final service-boundary recheck after the malformed authenticated cancellation input regression: `pytest tests/bridge tests/api/test_service_execution_contract.py -q`: **171 passed**, 12.89 seconds.
- `pytest tests/bridge/test_dispatch.py tests/core/storage/sqlalchemy/test_custom_task_model.py tests/core/storage/test_migration.py -q`: **43 passed**, including the storage files excluded by default.
- `ruff check --fix .`: passed. `black --check .`: **276 files unchanged**.
- `pyright . --pythonpath /private/tmp/apflow-findings-deps/bin/python`: **0 errors, 0 warnings**.
- `python -m pip check`: **No broken requirements found**. `git diff --check`: passed.

The test run uses synthetic executors, authenticated ASGI REST requests, temporary
SQLite databases, concurrent sessions/threads and mocked HTTP transports. It does
not establish real PostgreSQL cluster, world, network egress or provider billing
acceptance; operational boundaries above remain part of deployment validation.


## Approved Run and Accounting Revision

The user approved explicit Run boundaries, append-only usage facts, atomic local
accounting and retry-as-new-run. This supersedes the earlier generic resume and
in-place reconciliation design; existing completed-run and monetary summaries
must not be mistaken for idempotent billing evidence.

### High-Impact Decision Record: Run, invocation and usage persistence

Necessity: root task execution status is not a workflow lifecycle; copying a task
currently copies usage/cost and cumulative cancellation reports double count.
Preserve the structure tree and execution DAG, reuse TaskModel as per-run nodes,
ExecutionAttempt as invocation identity and the existing budget ledger.

Persistence: add WorkflowRun, immutable UsageEvent/outbox records, direct run and
source lineage and frozen-result hashes. A Run snapshots the accepted definition.
Task fields are projections; actual consumption belongs to invocation events.
Freeze terminal business history; allow authorized late accounting facts.

State/API: keep task creation and submission, add run.get/run.retry. Terminal Runs
never reset to pending. Retry creates fresh IDs atomically with a subject-scoped
receipt; completed dependencies become immutable zero-new-cost result references.
Unknown remote effects require explicit receipt evidence and replay-safety
confirmation before a new invocation. Dispatch only requeues never-started work.

Atomicity: invocation finalization, unique usage insertion, task projections,
reservation settlement and outbox publication state use one local DB transaction.
Every physical invocation has a stable identity shared with its reservation.
Repeated cumulative snapshots contribute only their confirmed delta; conflicting
reuse of an event key is rejected. Late old-generation usage cannot change current
execution status but remains billable historical evidence.

Migration: additive migration 007; historical task-only usage must not disappear
from reports. New projections use usage events, legacy projections remain clearly
identified; reused nodes do not inherit their source's billed amounts. Real
monetary balances remain owned by the billing system, outside orchestration.

Cheaper alternatives: continuing to mutate terminal task rows or copying their
fee fields cannot satisfy the reproduced invariants. A full WorkflowDefinition
and separate TaskRun-table rewrite adds duplicate models without immediate value.

### Revision Tasks

| Existing task | Ownership | Required evidence |
|---|---|---|
| scope-budget | usage_ledger | schema, atomic events/settlement, legacy accounting, duplicate/conflict/late/fault tests |
| durable-dispatch | run_lifecycle | run snapshot/lifecycle, no terminal replay, before/after-start crash rules |
| core-invariants | retry_runs | fresh clone identity/fees, frozen reuse, DAG retry scope and idempotent receipt |
| retry-errors / bootstrap-identity | main | TaskManager invocation/cancel wiring, event outbox API, authenticated integration, docs and full checks |

Implementation follows TDD and the existing matched dependency environment.
Only existing planning files are maintained for this revision.


### New Boundary Files

- `core/execution/runs.py`: accepted graph snapshots, lifecycle transitions and lease fencing.
- `core/execution/run_retry.py`: DAG scope, frozen result reuse and atomic idempotent derivation.
- `governance/usage.py`: immutable usage facts, projections, evidenced reconciliation and outbox delivery.
- `core/storage/migrations/007_add_workflow_runs_usage_events.py`: additive schema and legacy baseline recognition.

The accepted API requires structural-root execution. Child definitions should be
assembled before admission; standalone child execution and in-place task reset
are rejected. Redacted inline configuration cannot be replayed as a credential;
use configured credential references in a fresh definition.

### Revision Verification

- New Run/retry/usage core module coverage: **91%**, with **56 passed, 1 skipped**.
- Storage supplement excluded by default: **33 passed** (migration and custom model).
- Final retry, execution history, usage outbox and HTTP conflict checks: **30 passed**.
- Real PostgreSQL cluster, external billing consumer, world and provider acceptance
  remain deployment checks; no real remote side effects were invoked here.

- Full default suite: **1824 passed, 11 skipped, 34 warnings**, 266.28 seconds.
  Skips include optional aiosqlite and external PostgreSQL acceptance prerequisites.
- Final governance and invocation regression after budget denial/overrun history
  corrections: **140 passed, 1 skipped**; the two new assertions first failed with
  completed/running invocation states before their fixes.
- Ruff, Black (285 files), Pyright (0 errors/warnings), import graph (132 modules),
  dependency consistency and whitespace checks passed. Final formatting and commit
  hooks repeat the static gates before integration.
- Initial full regression exposed expectations for in-place reruns and child-only
  execution. Those assertions now verify explicit rejection, unchanged history,
  new Run derivation, and whole-root execution of builder-created children.

- Final complete source freeze recheck: **1824 passed, 11 skipped**; final Run,
  bridge and governance compatibility check: **348 passed, 1 skipped**.
- Final safeguards include immutable node identities/retry policy, atomic legacy
  Run enrollment, and redelivery of unacknowledged events below the cursor so
  out-of-order database commits cannot silently lose billing facts.
- Coverage recheck: **91.07%** across the three new core modules; **56 passed,
  1 skipped**. Submission hooks check all **52 changed files**, imports and Ruff.
