# Scope reservations and failed usage accounting

## Goal
APF-12: Extend BudgetManager with shared scope ledger, atomic per-attempt reservations and settlement; unknown usage holds reservation pending reconciliation; keep application quality/round decisions outside apflow.

## Files Involved
Modify existing governance/budget.py, models.py, task_manager.py; budget tests. New boundary files require the justification in plan.md Reuse Report.

## Steps
1. Reuse the relevant EXTEND/REUSE utilities and EXISTING_CONFIG from plan.md; inspect callers and existing pytest fixtures before edits.
2. Add failing regression tests for the reviewed behavior, including null/missing input, authorization and concurrency where relevant. Use EXISTING_TEST_SCAFFOLDING: SQLite fixtures, MockTransport and API/CLI clients.
3. Run the focused tests with `PYTHONPATH=src /private/tmp/apflow-findings-deps/bin/python -m pytest <tests> -q` and record the red failure.
4. Extend BudgetManager with shared scope ledger, atomic per-attempt reservations and settlement; unknown usage holds reservation pending reconciliation; keep application quality/round decisions outside apflow.
5. Run new and adjacent regression tests, refactor for clarity, then run lint/type checks. Avoid replacing working existing mechanisms or adding teaching policy.

## Acceptance Criteria
- [x] APF-12 acceptance conditions in source are covered by named tests.
- [x] Existing supported behavior remains passing.
- [x] Errors, cancellation and unknown-outcome paths are explicit.
- [x] Commands/results are recorded before completion status.

## Dependencies
Depends on: core-invariants, retry-errors. Required by: none.

## Estimated Time
8 hours.

## Design Decisions Reference
See plan.md § Design Decisions: Persistence, API, execution state, and cross-service boundaries for the High-Impact Decision Record.

## Verification
See [plan.md](../plan.md#final-verification) for named APF coverage, final commands, results, and deployment boundaries.
