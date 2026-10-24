# Atomic idempotent task submission

## Goal
APF-06: Reuse idempotency_key; add namespace/owner request digest receipt with atomic task-tree commit, repeat lookup and explicit conflict.

## Files Involved
Modify existing bridge/task_modules.py, core/storage/sqlalchemy/models.py, task_repository.py; submission tests. New boundary files require the justification in plan.md Reuse Report.

## Steps
1. Reuse the relevant EXTEND/REUSE utilities and EXISTING_CONFIG from plan.md; inspect callers and existing pytest fixtures before edits.
2. Add failing regression tests for the reviewed behavior, including null/missing input, authorization and concurrency where relevant. Use EXISTING_TEST_SCAFFOLDING: SQLite fixtures, MockTransport and API/CLI clients.
3. Run the focused tests with `PYTHONPATH=src /private/tmp/apflow-findings-deps/bin/python -m pytest <tests> -q` and record the red failure.
4. Reuse idempotency_key; add namespace/owner request digest receipt with atomic task-tree commit, repeat lookup and explicit conflict.
5. Run new and adjacent regression tests, refactor for clarity, then run lint/type checks. Avoid replacing working existing mechanisms or adding teaching policy.

## Acceptance Criteria
- [x] APF-06 acceptance conditions in source are covered by named tests.
- [x] Existing supported behavior remains passing.
- [x] Errors, cancellation and unknown-outcome paths are explicit.
- [x] Commands/results are recorded before completion status.

## Dependencies
Depends on: bootstrap-identity. Required by: durable-dispatch.

## Estimated Time
6 hours.

## Design Decisions Reference
See plan.md § Design Decisions: Persistence, API, execution state, and cross-service boundaries for the High-Impact Decision Record.

## Verification
See [plan.md](../plan.md#final-verification) for named APF coverage, final commands, results, and deployment boundaries.
