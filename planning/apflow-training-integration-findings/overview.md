# Training Integration Findings

## Overview
Implement the fourteen verified defects and service contract increments without embedding training business rules.

## Scope
Includes orchestration invariants, identity, bootstrap, network/credential/readiness policy, submissions, durable dispatch, errors/retries and scope budgets. Excludes pedagogy, world migrations, real provider billing guarantees and remote side-effect rollback.

## Technology Stack
Python, SQLAlchemy SQLite/PostgreSQL, apcore bridges, httpx, Click; pytest, ruff, Black and Pyright.

## Task Execution Order
| # | Task File | Description | Status |
|---|---|---|---|
| 1 | [core-invariants](./tasks/core-invariants.md) | Reference results, archives, and actual cancellation | completed |
| 2 | [bootstrap-identity](./tasks/bootstrap-identity.md) | Bootstrap, identity boundaries, and dependency contract | completed |
| 3 | [outbound-readiness](./tasks/outbound-readiness.md) | Target policy, credential references, and readiness | completed |
| 4 | [idempotent-submission](./tasks/idempotent-submission.md) | Atomic idempotent task submission | completed |
| 5 | [durable-dispatch](./tasks/durable-dispatch.md) | Persistent detached dispatch and recovery | completed |
| 6 | [retry-errors](./tasks/retry-errors.md) | Retry classification and structured errors | completed |
| 7 | [scope-budget](./tasks/scope-budget.md) | Scope reservations and failed usage accounting | completed |

## Progress
Total: 7; completed: 7; in_progress: 0; pending: 0.

## Reference Documents
[Source findings](/Users/tercelyi/Workspace/corefirst/corefirst-world/docs/apflow-training-integration-findings.md) · [Plan](./plan.md)
