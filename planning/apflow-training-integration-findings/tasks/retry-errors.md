# Retry classification and structured errors

## Goal
APF-09, APF-10: Extend current RetryPolicy with predicate and cancellation; align canonical envelope failure classification and correlation metadata; keep business rejection successful.

## Files Involved
Modify existing durability/retry.py, execution/result_protocol.py, task_manager.py, RestExecutor; retry tests. New boundary files require the justification in plan.md Reuse Report.

## Steps
1. Reuse the relevant EXTEND/REUSE utilities and EXISTING_CONFIG from plan.md; inspect callers and existing pytest fixtures before edits.
2. Add failing regression tests for the reviewed behavior, including null/missing input, authorization and concurrency where relevant. Use EXISTING_TEST_SCAFFOLDING: SQLite fixtures, MockTransport and API/CLI clients.
3. Run the focused tests with `PYTHONPATH=src /private/tmp/apflow-findings-deps/bin/python -m pytest <tests> -q` and record the red failure.
4. Extend current RetryPolicy with predicate and cancellation; align canonical envelope failure classification and correlation metadata; keep business rejection successful.
5. Run new and adjacent regression tests, refactor for clarity, then run lint/type checks. Avoid replacing working existing mechanisms or adding teaching policy.

## Acceptance Criteria
- [x] APF-09, APF-10 acceptance conditions in source are covered by named tests.
- [x] Existing supported behavior remains passing.
- [x] Errors, cancellation and unknown-outcome paths are explicit.
- [x] Commands/results are recorded before completion status.

## Dependencies
Depends on: core-invariants. Required by: scope-budget.

## Estimated Time
6 hours.

## Design Decisions Reference
See plan.md § Design Decisions: Persistence, API, execution state, and cross-service boundaries for the High-Impact Decision Record.

## Verification
See [plan.md](../plan.md#final-verification) for named APF coverage, final commands, results, and deployment boundaries.

## Approved Run Revision

The accepted revision replaces resetting old tasks with `run.retry` and requires
root execution for a managed graph. Unknown outcomes require replay evidence.
`test_task_reexecution.py` verifies new identities, retained original failures,
unbilled dependency reuse and terminal rejection. Error taxonomy tests verify
transport status 409 for Run conflicts.
