# Bootstrap, identity boundaries, and dependency contract

## Goal
APF-07, APF-13, APF-14: Parse storage configuration before bootstrap; bind authenticated identity to all task operations and references; retain explicit trusted library mode; use declared compatible dependencies and upstream validation fields.

## Files Involved
Modify existing app.py, cli.py, api/auth.py, bridge/access.py, bridge/task_modules.py; API/CLI tests. New boundary files require the justification in plan.md Reuse Report.

## Steps
1. Reuse the relevant EXTEND/REUSE utilities and EXISTING_CONFIG from plan.md; inspect callers and existing pytest fixtures before edits.
2. Add failing regression tests for the reviewed behavior, including null/missing input, authorization and concurrency where relevant. Use EXISTING_TEST_SCAFFOLDING: SQLite fixtures, MockTransport and API/CLI clients.
3. Run the focused tests with `PYTHONPATH=src /private/tmp/apflow-findings-deps/bin/python -m pytest <tests> -q` and record the red failure.
4. Parse storage configuration before bootstrap; bind authenticated identity to all task operations and references; retain explicit trusted library mode; use declared compatible dependencies and upstream validation fields.
5. Run new and adjacent regression tests, refactor for clarity, then run lint/type checks. Avoid replacing working existing mechanisms or adding teaching policy.

## Acceptance Criteria
- [x] APF-07, APF-13, APF-14 acceptance conditions in source are covered by named tests.
- [x] Existing supported behavior remains passing.
- [x] Errors, cancellation and unknown-outcome paths are explicit.
- [x] Commands/results are recorded before completion status.

## Dependencies
Depends on: none. Required by: idempotent-submission, durable-dispatch.

## Estimated Time
8 hours.

## Design Decisions Reference
See plan.md § Design Decisions: Persistence, API, execution state, and cross-service boundaries for the High-Impact Decision Record.

## Verification
See [plan.md](../plan.md#final-verification) for named APF coverage, final commands, results, and deployment boundaries.
