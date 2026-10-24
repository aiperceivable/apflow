# Reference results, archives, and actual cancellation

## Goal
APF-02, APF-03, APF-05: Resolve effective Link data once, retain Link IDs and deepcopy/null semantics; reject archived writes/claims atomically; share execution cancellation signals and conditional terminal updates.

## Files Involved
Modify existing task_repository.py, task_creator.py, task_executor.py, task_manager.py; core regressions. New boundary files require the justification in plan.md Reuse Report.

## Steps
1. Reuse the relevant EXTEND/REUSE utilities and EXISTING_CONFIG from plan.md; inspect callers and existing pytest fixtures before edits.
2. Add failing regression tests for the reviewed behavior, including null/missing input, authorization and concurrency where relevant. Use EXISTING_TEST_SCAFFOLDING: SQLite fixtures, MockTransport and API/CLI clients.
3. Run the focused tests with `PYTHONPATH=src /private/tmp/apflow-findings-deps/bin/python -m pytest <tests> -q` and record the red failure.
4. Resolve effective Link data once, retain Link IDs and deepcopy/null semantics; reject archived writes/claims atomically; share execution cancellation signals and conditional terminal updates.
5. Run new and adjacent regression tests, refactor for clarity, then run lint/type checks. Avoid replacing working existing mechanisms or adding teaching policy.

## Acceptance Criteria
- [x] APF-02, APF-03, APF-05 acceptance conditions in source are covered by named tests.
- [x] Existing supported behavior remains passing.
- [x] Errors, cancellation and unknown-outcome paths are explicit.
- [x] Commands/results are recorded before completion status.

## Dependencies
Depends on: none. Required by: durable-dispatch, retry-errors, scope-budget.

## Estimated Time
8 hours.

## Design Decisions Reference
See plan.md § Design Decisions: Persistence, API, execution state, and cross-service boundaries for the High-Impact Decision Record.

## Verification
See [plan.md](../plan.md#final-verification) for named APF coverage, final commands, results, and deployment boundaries.

## Approved Run Revision

The accepted revision derives a new Run with direct source lineage and immutable
completed-result snapshots. Generic Copy resets execution and billing fields; Link
has no inherited charges. `test_run_retry.py` covers unchanged original history,
DAG scope, downstream invalidation, frozen results, ownership, concurrent idempotent
retry, transaction rollback, and accepted/terminal ORM invariants.
