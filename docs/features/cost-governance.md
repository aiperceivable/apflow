---
description: Feature specification for token budgets, cost policies, model downgrade chains, and usage reporting in apflow.
---

# Feature Spec: Cost Governance (F-004)

**Feature ID:** F-004
**Priority:** P0
**Phase:** Phase 3 (0.20.0-beta.1)
**Tech Design Reference:** Section 4.5

---

## Purpose

Provide token budget management and cost policy enforcement for AI agent tasks. Enables per-task budgets with configurable actions (block, downgrade model, notify) when budgets are approached or exceeded. Token usage data comes from the executor's `execute()` return value, making this framework-agnostic.

---

## Current Behavior and Cost Data Sources

Legacy per-task budget checks run before each task execution. They use usage already reported by
previous executions; they do not interrupt a running executor or reserve tokens
for its next request. A single execution can therefore exceed the remaining
budget. `NOTIFY` and `CONTINUE` policies permit execution after their threshold,
including after exhaustion; `DOWNGRADE` passes the selected model in
`inputs["model"]`, which the executor must honor.

That budget enforcement is per task. `BudgetScope.USER` is reserved for representing a
user scope; user-wide budget enforcement is not implemented. `UsageReporter`
already provides per-user usage aggregation.

For a shared experiment or task group, configure a token account with
`BudgetLedger.configure_scope(scope_id, limit_tokens)` and bind the trusted
`budget_scope_id` to its member tasks. Service callers use owner-scoped public
names; `scope_budget_key()` derives an internal identifier from the authenticated
owner and public name. Repeating configuration verifies the existing limit;
it does not reset spending or increase an existing account's allowance.

Scoped executions require a registered executor's synchronous
`token_reservation_bound(inputs) -> int` method. The executor must enforce this
bound across all provider calls inside that invocation. A task-supplied estimate
or `max_tokens` field alone is insufficient. If that contract is absent,
execution fails before calling the executor, including when the optional legacy
governance policy manager was not configured. Generic HTTP executors cannot
establish a remote service's hidden model consumption; use a trusted bounded
adapter before adding such calls to a strict scope.

Before each invocation, including every retry, a conditional database update
reserves tokens only when `spent + reserved + unknown + requested <= limit`.
SQLite serializes the balance mutation; PostgreSQL uses the same conditional
update and transactional reservation insert. Reported usage settles the
reservation exactly once. Failed invocations can report billable usage. Missing
usage, lost receipts, cancellation, and exceptions without counters retain an
unknown liability equal to the reservation; a restart or timeout never releases
it automatically. A hard process exit can leave that held record marked
`reserved` until reconciliation; it still reduces available capacity.
Authorized reconciliation settles the original reservation
with actual usage. Task usage accounting remains separate from this account and
includes each attempt without counting the final result twice.

These are token admission and accounting guarantees. Actual consumption can
exceed a declared bound if an executor violates its contract; recorded usage is
never truncated to hide that violation, and the invocation fails with a
configuration error when reported tokens exceed its declared bound. Strict monetary limits require a
provider-enforced upper bound, current pricing, and all billable operations to
participate. The ledger does not promise a hard dollar ceiling for hidden calls
or unknown provider charges. Fixed-model experiments must choose blocking
policies rather than model downgrade policies. Model downgrade requires the
task to explicitly select a registered policy allowing that action; sharing an
account does not select a downgrade policy.

New executors report counters outside their business result in the explicit
`{result, error?, error_info?, token_usage?}` execution envelope. The function decorator wraps
raw business returns automatically; functions reporting execution usage declare
`result_format="envelope"`. Usage is stored separately from business output, even
without budget enforcement. Legacy executor classes retain their old top-level
usage convention. See the [result protocol](../architecture/task-orchestration.md#executor-result-protocol).

The application supplies `estimated_cost_usd` and `actual_cost_usd`, for example
using the real repository attached to its execution session:

```python
from decimal import Decimal

await repository.update_task(
    task_id,
    estimated_cost_usd=Decimal("0.020000"),
    actual_cost_usd=Decimal("0.012345"),
)
```

These fields are stored amounts, not values inferred from token counts. Returning
an `actual_cost_usd` business field from an executor alone does not populate the
model column. Provider pricing and predictive estimates remain future work
([PRD F-009](../prd.md#f-009-advanced-cost-strategies)). Usage reports sum stored
actual costs; an unset cost contributes zero to the aggregate, which does not
establish that the execution was free. An explicit zero amount remains `0.0`
when the task is serialized, while an unset amount remains `null`.
Reports preserve numeric aggregates for compatibility, and additionally expose
`unknown_cost_task_count`, `unknown_usage_task_count`, and `cost_complete` so an
unknown cost is distinguishable from an explicitly reported zero. Shared-scope
status and its reservation records separately show outstanding and unknown
token liabilities.
Manual reconciliation stores its first evidence reference, operator identity,
observation time, and actual token amount atomically with the settlement in an
immutable submission receipt. Repeating settlement preserves that evidence;
changing the actual amount conflicts instead of rewriting the old charge.

---

## File Changes

### New Files

**`src/apflow/governance/__init__.py`**

```python
"""Cost governance: budget tracking, policy enforcement, provider routing, usage reporting."""

from apflow.governance.budget import TokenBudget, BudgetManager, BudgetScope, BudgetCheckResult
from apflow.governance.policy import CostPolicy, PolicyEngine, PolicyAction, PolicyEvaluation
from apflow.governance.provider_router import ProviderRouter, ModelSelection
from apflow.governance.reporter import UsageReporter, UsageSummary

__all__ = [
    "TokenBudget", "BudgetManager", "BudgetScope", "BudgetCheckResult",
    "CostPolicy", "PolicyEngine", "PolicyAction", "PolicyEvaluation",
    "ProviderRouter", "ModelSelection",
    "UsageReporter", "UsageSummary",
]
```

**`src/apflow/governance/budget.py`** -- BudgetScope enum, TokenBudget dataclass, BudgetCheckResult dataclass, BudgetManager class.
**`src/apflow/governance/policy.py`** -- PolicyAction enum, CostPolicy dataclass, PolicyEvaluation dataclass, PolicyEngine class.
**`src/apflow/governance/provider_router.py`** -- ModelSelection dataclass, ProviderRouter class.
**`src/apflow/governance/reporter.py`** -- UsageSummary dataclass, UsageReporter class.

### Modified Files

**`src/apflow/core/storage/sqlalchemy/models.py`**

Add to `TaskModel` (shares migration 004 with F-003):

```python
# === Cost Governance Fields (F-004) ===
token_usage = Column(JSON, nullable=True)
token_budget = Column(Integer, nullable=True)
estimated_cost_usd = Column(Numeric(12, 6), nullable=True)
actual_cost_usd = Column(Numeric(12, 6), nullable=True)
cost_policy = Column(String(100), nullable=True)
```

Update `to_dict()` to include: `token_usage`, `token_budget`, `estimated_cost_usd`, `actual_cost_usd`, `cost_policy`.

**`src/apflow/core/execution/task_manager.py`**

1. Add `__init__()` parameters: `budget_manager: Optional[BudgetManager] = None`, `policy_engine: Optional[PolicyEngine] = None`.
2. Before executor creation, `_apply_budget_policy(task, inputs)` checks
   the task budget and evaluates a configured policy at its utilization threshold.
   A threshold of `0.8` takes effect at 80% utilization, even with tokens remaining.
   An unknown policy name fails the task as a configuration error. Tasks without a
   budget skip utilization-based policies; exhausted tasks without a policy fail.
3. `_handle_task_execution_result()` accumulates any reported `token_usage` before
   classifying the returned result as successful or failed. A failed result can
   still represent billable work. Exceptions without usage metadata cannot be
   accounted for automatically.

### Test Files

```
tests/governance/__init__.py
tests/governance/test_budget.py
tests/governance/test_policy.py
tests/governance/test_provider_router.py
tests/governance/test_reporter.py
tests/governance/test_integration.py
```

---

## Method Signatures

### TokenBudget

```python
@dataclass
class TokenBudget:
    scope: BudgetScope       # BudgetManager enforces TASK only; USER is reserved
    scope_id: str            # Non-empty string
    limit: int               # >= 0; zero means immediately exhausted
    used: int = 0            # >= 0

    @property
    def remaining(self) -> int:
        """max(0, limit - used)"""

    @property
    def utilization(self) -> float:
        """used / limit (0.0 to 1.0+). Returns 1.0 if limit is 0."""

    @property
    def is_exhausted(self) -> bool:
        """used >= limit"""
```

### BudgetManager

```python
class BudgetManager:
    def __init__(self, task_repository: Any) -> None: ...

    async def check_budget(self, task_id: str) -> BudgetCheckResult:
        """Check if task has remaining budget.
        Logic:
        1. Validate task_id non-empty.
        2. Get task from repository. Raise KeyError if not found.
        3. If task.token_budget is None: return allowed=True, remaining=-1, utilization=-1.0 (unlimited; -1.0 means no budget configured).
        4. Get current usage from task.token_usage.total (default 0).
        5. Create TokenBudget.
        6. Return BudgetCheckResult(allowed=not exhausted, remaining, utilization).
        """

    async def update_usage(self, task_id: str, token_usage: Dict[str, int]) -> TokenBudget | None:
        """Update usage after execution.
        Logic:
        1. Validate task_id non-empty.
        2. Validate token_usage values >= 0 for keys input, output, total.
        3. Get task. Raise KeyError if not found.
        4. Accumulate: existing.input + new.input, existing.output + new.output, existing.total + new.total.
        5. Update task via repository.
        6. If no budget configured, return None.
        7. Return updated TokenBudget.
        """
```

`update_usage` supports legacy tasks without a Run, invocation history, or usage
events. For managed execution, it raises `ValueError`: record consumption through
`UsageRecorder` with stable invocation and event IDs so reporting, deduplication,
and budget settlement use the same facts.
New facts for a terminal invocation require `UsageRecorder.reconcile` with evidence;
`record` only accepts an identical existing receipt replay after finalization.

### CostPolicy

```python
@dataclass(frozen=True)
class CostPolicy:
    name: str                  # Non-empty string
    action: PolicyAction       # BLOCK, DOWNGRADE, NOTIFY, CONTINUE
    threshold: float           # 0.0 < x <= 1.0
    downgrade_chain: list[str] = field(default_factory=list)  # Required if action == DOWNGRADE
    description: str = ""
```

### PolicyEngine

```python
class PolicyEngine:
    def __init__(self) -> None: ...

    def register_policy(self, policy: CostPolicy) -> None:
        """Register policy. Raises ValueError if name already exists."""

    def get_policy(self, name: str) -> Optional[CostPolicy]:
        """Get policy by name. Returns None if not found."""

    def evaluate(
        self,
        policy_name: str,        # Must be registered
        utilization: float,      # >= 0.0
        current_model_index: int = 0,  # >= 0
    ) -> PolicyEvaluation:
        """Evaluate policy against utilization.
        Logic:
        1. Validate utilization >= 0.
        2. Get policy. Raise KeyError if not registered.
        3. triggered = utilization >= policy.threshold.
        4. If not triggered: return action=CONTINUE.
        5. If triggered:
           - BLOCK: return action=BLOCK.
           - DOWNGRADE: next_index = current_model_index + 1.
             If next_index < len(chain): return action=DOWNGRADE, model_override=chain[next_index].
             Else: return action=BLOCK (chain exhausted).
           - NOTIFY: return action=NOTIFY.
           - CONTINUE: return action=CONTINUE.
        """
```

### ProviderRouter

```python
class ProviderRouter:
    def select_model(
        self,
        downgrade_chain: list[str],  # At least 1 entry
        current_index: int = 0,       # >= 0, < len(chain)
    ) -> ModelSelection:
        """Select model at current_index.
        Raises: ValueError if chain empty or index out of bounds.
        """

    def get_next_model(
        self,
        downgrade_chain: list[str],
        current_index: int,
    ) -> Optional[ModelSelection]:
        """Get next cheaper model. Returns None if chain exhausted."""
```

### UsageReporter

```python
class UsageReporter:
    def __init__(self, task_repository: Any) -> None: ...

    async def get_task_usage(self, task_id: str) -> UsageSummary:
        """Usage for single task. Raises: ValueError if empty, KeyError if not found."""

    async def get_user_usage(
        self,
        user_id: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> UsageSummary:
        """Aggregated usage for user over period.
        Raises: ValueError if empty or end < start.
        """

    def export_json(self, summary: UsageSummary) -> str:
        """Export as JSON string with indent=2."""
```

---

## Public Budget and Usage Operations

The default registry namespace exposes the same operations through REST and CLI:

| REST module (`POST /modules/<id>`) | Local CLI | Required input |
|---|---|---|
| `apflow.budget.scope` | `apflow budget scope` | `scope`; optional `limit_tokens` to configure |
| `apflow.budget.reconcile` | `apflow budget reconcile` | `scope`, `reservation_id`, `actual_tokens`, `evidence_ref` |
| `apflow.usage.events` | `apflow usage events` | Optional `after_index` (default 0), `limit` (1–1000, default 100) |
| `apflow.usage.ack` | `apflow usage ack` | `event_id` |

CLI module commands operate on the local database selected by
`APFLOW_DATABASE_URL`; use authenticated REST calls when operating a remote
service. Public scope names are bound to the verified owner. Only administrators
may select another `owner_id` or reconcile consumption on authenticated services.
Local CLI identity/role assertions are not service authentication.

```bash
# Configure once; repeating the same limit preserves accumulated spending.
apflow budget scope --scope experiment-1 --limit-tokens 10000
apflow budget scope --scope experiment-1

# Read consumption facts. Save the returned next_index after processing the page.
apflow usage events --after-index 0 --limit 100

# Only after the consuming application has durably committed this event.
apflow usage ack --event-id EVENT_ID

# Trusted local operator: reconcile a reservation with a confirmed provider receipt.
APCORE_CLI_AUTO_APPROVE=1 apflow budget reconcile --scope experiment-1 \
  --reservation-id INVOCATION_ID --actual-tokens 120 \
  --evidence-ref receipt:provider-operation-1
```

The scope response includes internal `scope_id`, `limit_tokens`, `spent_tokens`,
`reserved_tokens`, `unknown_tokens`, `remaining_tokens`, and `reservations`.
The `task.create` / `task.create_tree` modules and their CLI commands accept the
public scope name as `budget_scope_id` and derive the internal owner-scoped key,
including for trusted local CLI calls. Code using `TaskCreator` or the repository
directly must bind that internal key itself. Configuring a scope alone does not
enroll existing tasks. A bounded executor is still required. CLI reconciliation
requires confirmation. With the current apcore-cli 0.12.5 baseline, `--yes` clears
the CLI check but does not reach the executor's second approval gate. For a
reviewed noninteractive operation, the command-scoped `APCORE_CLI_AUTO_APPROVE=1`
shown above confirms both gates without granting administrator privileges. Do
not export it as a session-wide default. Interactive approval remains available.

Equivalent REST requests (add the verified Bearer token on authenticated services):

```bash
curl -X POST localhost:8080/modules/apflow.budget.scope \
  -H 'content-type: application/json' \
  -d '{"scope":"experiment-1","limit_tokens":10000}'

curl -X POST localhost:8080/modules/apflow.usage.events \
  -H 'content-type: application/json' -d '{"after_index":0,"limit":100}'

curl -X POST localhost:8080/modules/apflow.usage.ack \
  -H 'content-type: application/json' -d '{"event_id":"EVENT_ID"}'

# Requires an administrator on an authenticated service.
curl -X POST localhost:8080/modules/apflow.budget.reconcile \
  -H 'content-type: application/json' \
  -d '{"scope":"experiment-1","reservation_id":"INVOCATION_ID","actual_tokens":120,"evidence_ref":"receipt:provider-operation-1"}'
```

`ExecutionAttempt.id` is the invocation and reservation ID. Budget reconciliation
settles that original invocation and can append evidenced correction facts; it
does not reset tasks, change their outcomes, or create another execution. Use
`run.retry` for a new execution Run. Missing counters remain unknown; explicitly
reported zero is known. Late facts remain attributed to the original Run.

`usage.events` returns `events` and a monotonic `next_index`. Delivery is at least
once: unacknowledged events can reappear even below the requested cursor. The
consumer must atomically deduplicate by event `id` and commit its business writes
before calling `usage.ack`. Repeating an acknowledgement is safe. Corrections
may contain signed token deltas and must be processed as events, not as fresh
positive-only charges. Imported legacy baseline events are excluded from this
delivery feed.

apflow stores consumption facts and token-budget balances. The consuming
application owns prices, credits, monetary transactions, refunds, and wallets;
these commands do not start a billing consumer or wallet service.

The same operations are available as MCP tools and A2A skills. Use apflow's
protocol entry points to preserve token counters while redacting credentials.
A2A exact integer inputs are normalized at the bridge boundary; larger accounting
values need a transport that preserves integers. See
[MCP and A2A operations](apcore-bridge.md#mcp-and-a2a-operations).

---

## Data Models

### TaskModel Additions

| Field | SQLAlchemy Type | Python Type | Default | Validation |
|---|---|---|---|---|
| `token_usage` | `JSON` | `Optional[dict]` | `None` | `{input: int>=0, output: int>=0, total: int>=0}` |
| `token_budget` | `Integer` | `Optional[int]` | `None` | `>= 0`; zero means exhausted |
| `estimated_cost_usd` | `Numeric(12,6)` | `Optional[float]` | `None` | `>= 0` when set |
| `actual_cost_usd` | `Numeric(12,6)` | `Optional[float]` | `None` | `>= 0` when set |
| `cost_policy` | `String(100)` | `Optional[str]` | `None` | Must be a registered policy name |

---

## Test Requirements

### Unit Tests: `tests/governance/test_budget.py`

```python
def test_token_budget_defaults():
    """TokenBudget with limit=100, used=0: remaining=100, utilization=0.0, not exhausted."""

def test_token_budget_remaining():
    """limit=1000, used=600: remaining=400."""

def test_token_budget_remaining_over_limit():
    """limit=100, used=150: remaining=0 (not negative)."""

def test_token_budget_utilization():
    """limit=1000, used=800: utilization=0.8."""

def test_token_budget_utilization_over():
    """limit=100, used=150: utilization=1.5."""

def test_token_budget_is_exhausted():
    """limit=100, used=100: is_exhausted=True. used=99: False."""

def test_token_budget_limit_zero_is_exhausted():
    """limit=0 is immediately exhausted with utilization=1.0."""

def test_token_budget_negative_used_raises():
    """used=-1 raises ValueError."""

def test_token_budget_empty_scope_id_raises():
    """Empty scope_id raises ValueError."""

async def test_budget_manager_check_no_budget():
    """Task with no token_budget returns allowed=True, remaining=-1."""

async def test_budget_manager_check_within_budget():
    """Task with budget=1000, used=500: allowed=True, remaining=500."""

async def test_budget_manager_check_exhausted():
    """Task with budget=100, used=100: allowed=False, remaining=0."""

async def test_budget_manager_check_empty_id_raises():
    """Empty task_id raises ValueError."""

async def test_budget_manager_check_not_found_raises():
    """Non-existent task_id raises KeyError."""

# The following are methods of TestBudgetManager class, not top-level functions:

class TestBudgetManager:
    async def test_update_accumulates(self):
        """Single update accumulates: existing {input:100, output:200, total:300}
        + new {input:50, output:50, total:100} → result.used == 400.
        Persistence: update_task called with {input:150, output:250, total:400}.
        """

    async def test_update_negative_raises(self):
        """Negative token_usage value raises ValueError."""
        with pytest.raises(ValueError, match=">= 0"):
            await bm.update_usage("t1", {"input": -1, "output": 0, "total": -1})

    async def test_update_returns_none_no_budget(self):
        """Returns None when no budget is configured."""
```

### Unit Tests: `tests/governance/test_policy.py`

```python
def test_cost_policy_valid():
    """Valid policy creation."""
    policy = CostPolicy(name="default", action=PolicyAction.BLOCK, threshold=0.8)
    assert policy.name == "default"

def test_cost_policy_empty_name_raises():
    """Empty name raises ValueError."""

def test_cost_policy_threshold_zero_raises():
    """threshold=0.0 raises ValueError (must be > 0.0)."""

def test_cost_policy_threshold_above_one_raises():
    """threshold=1.1 raises ValueError."""

def test_cost_policy_threshold_one_valid():
    """threshold=1.0 is valid."""

def test_cost_policy_downgrade_no_chain_raises():
    """action=DOWNGRADE with empty downgrade_chain raises ValueError."""

def test_policy_engine_register_and_get():
    """Register policy, retrieve by name."""

def test_policy_engine_register_duplicate_raises():
    """Duplicate name raises ValueError."""

def test_policy_engine_evaluate_not_triggered():
    """utilization=0.5 with threshold=0.8: not triggered, action=CONTINUE."""

def test_policy_engine_evaluate_block():
    """utilization=0.85 with threshold=0.8 and action=BLOCK: triggered, action=BLOCK."""

def test_policy_engine_evaluate_downgrade():
    """utilization=0.85, action=DOWNGRADE, chain=["opus","sonnet","haiku"], index=0:
    model_override="sonnet"."""

def test_policy_engine_evaluate_downgrade_chain_exhausted():
    """utilization=0.85, action=DOWNGRADE, chain=["opus"], index=0:
    action=BLOCK (no next model)."""

def test_policy_engine_evaluate_notify():
    """action=NOTIFY: triggered=True, action=NOTIFY, no model_override."""

def test_policy_engine_evaluate_threshold_boundary():
    """utilization=0.79999 with threshold=0.8: not triggered.
    utilization=0.8 with threshold=0.8: triggered."""

def test_policy_engine_evaluate_not_found_raises():
    """Non-existent policy name raises KeyError."""

def test_policy_engine_evaluate_negative_utilization_raises():
    """Negative utilization raises ValueError."""
```

### Unit Tests: `tests/governance/test_provider_router.py`

```python
def test_select_model_first():
    """index=0 returns first model, is_downgraded=False."""
    router = ProviderRouter()
    result = router.select_model(["opus", "sonnet", "haiku"], 0)
    assert result.model == "opus"
    assert not result.is_downgraded

def test_select_model_downgraded():
    """index=1 returns second model, is_downgraded=True."""
    router = ProviderRouter()
    result = router.select_model(["opus", "sonnet", "haiku"], 1)
    assert result.model == "sonnet"
    assert result.is_downgraded

def test_select_model_last():
    """index=2 on 3-element chain returns last model."""
    router = ProviderRouter()
    result = router.select_model(["opus", "sonnet", "haiku"], 2)
    assert result.model == "haiku"

def test_select_model_empty_chain_raises():
    """Empty chain raises ValueError."""
    with pytest.raises(ValueError, match="at least 1"):
        ProviderRouter().select_model([], 0)

def test_select_model_negative_index_raises():
    """Negative index raises ValueError."""
    with pytest.raises(ValueError):
        ProviderRouter().select_model(["opus"], -1)

def test_select_model_out_of_bounds_raises():
    """Index >= len(chain) raises ValueError."""
    with pytest.raises(ValueError, match="out of bounds"):
        ProviderRouter().select_model(["opus", "sonnet"], 2)

def test_get_next_model():
    """Returns next model in chain."""
    result = ProviderRouter().get_next_model(["opus", "sonnet", "haiku"], 0)
    assert result.model == "sonnet"

def test_get_next_model_exhausted():
    """Returns None when at last model."""
    result = ProviderRouter().get_next_model(["opus", "sonnet"], 1)
    assert result is None

def test_single_model_chain():
    """Single-model chain: select returns it, get_next returns None."""
    router = ProviderRouter()
    result = router.select_model(["opus"], 0)
    assert result.model == "opus"
    assert router.get_next_model(["opus"], 0) is None
```

### Unit Tests: `tests/governance/test_reporter.py`

```python
async def test_get_task_usage():
    """Returns usage for single task."""

async def test_get_task_usage_no_usage():
    """Task with no token_usage returns zeros."""

async def test_get_task_usage_not_found_raises():
    """Non-existent task raises KeyError."""

async def test_get_user_usage_aggregation():
    """Aggregates usage across multiple tasks for a user."""
    # Create 3 tasks for user "u1" with usage {total: 100}, {total: 200}, {total: 300}
    # Verify: total_tokens=600, task_count=3

async def test_get_user_usage_empty():
    """No tasks for user returns zeros and task_count=0."""

async def test_get_user_usage_invalid_period_raises():
    """end_time < start_time raises ValueError."""

def test_export_json_format():
    """Exports valid JSON with all summary fields."""
    summary = UsageSummary(scope="task", scope_id="t1", total_input_tokens=100,
                           total_output_tokens=200, total_tokens=300,
                           total_cost_usd=0.015, task_count=1,
                           period_start=None, period_end=None)
    json_str = UsageReporter(mock_repo).export_json(summary)
    data = json.loads(json_str)
    assert data["total_tokens"] == 300
    assert data["total_cost_usd"] == 0.015
```

### Integration Tests: `tests/governance/test_integration.py`

```python
# --- Wiring: TaskExecutor builds governance components only when enabled ---

def test_build_components_governance_enabled(sync_db_session):
    """TaskExecutor._build_execution_components() includes budget_manager and
    policy_engine when governance is enabled via set_governance_enabled(True)."""

def test_build_components_disabled_by_default(sync_db_session):
    """With no config changes, components == {} (no governance or durability)."""

# --- Real async BudgetManager against a real repository (not a sync mock) ---

async def test_budget_check_allows_under_limit(sync_db_session):
    """Task with token_budget=1000, token_usage={total:400}: allowed=True, remaining=600."""

async def test_budget_check_blocks_when_exhausted(sync_db_session):
    """Task with token_budget=100, token_usage={total:100}: allowed=False, remaining=0."""

async def test_budget_check_unlimited_without_budget(sync_db_session):
    """Task with no token_budget: allowed=True, remaining=-1 (unlimited)."""

async def test_update_usage_accumulates_and_persists(sync_db_session):
    """Task with existing {total:300} + update {total:100} → DB token_usage["total"] == 400."""

# --- Policy evaluation drives the block/downgrade decision on exhaustion ---

def test_policy_blocks_on_exhaustion():
    """CostPolicy(action=BLOCK, threshold=0.8) with utilization=1.0 → BLOCK."""

def test_policy_downgrades_to_next_model():
    """CostPolicy(action=DOWNGRADE, chain=["opus","sonnet","haiku"]) at index=0,
    utilization=0.85 → DOWNGRADE with model_override="sonnet"."""

# --- End-to-end enforcement: a budget-exhausted task is blocked during execution ---

async def test_budget_exhausted_task_blocked_end_to_end(sync_db_session):
    """A token_budget-exhausted task run through TaskManager (with BudgetManager +
    PolicyEngine injected directly as constructor args) is marked failed with a
    budget error before the executor runs. Confirms the enforcement path closes
    the gap between check_budget returning not-allowed and the task actually failing."""
```

---

## Execution Wiring

Governance (and durability, F-003) lives in `TaskManager`, but every execution
path runs through the singleton `TaskExecutor`, which builds a fresh `TaskManager`
per execution. Two wiring paths exist:

### Path 1: Direct injection in `create_app()` (app.py lines 99–110)

`create_app()` instantiates `BudgetManager` and `PolicyEngine` and passes them
directly as constructor arguments to the `TaskManager` it creates:

```python
budget_manager = BudgetManager(task_repository)
policy_engine = PolicyEngine()
task_manager = TaskManager(
    session,
    budget_manager=budget_manager,
    policy_engine=policy_engine,
)
```

This governs the `TaskManager` used by the `task.cancel` module and by any
caller that uses the `app.task_manager` reference directly.

### Path 2: ConfigRegistry flags (for TaskExecutor — every per-request execution)

`create_app()` also registers the process-singletons and enables governance
globally so that `TaskExecutor` picks them up for every request execution:

- **app.py** calls `set_policy_engine(policy_engine)`, `set_governance_enabled(True)`,
  `set_circuit_breaker_registry(...)`, and `set_durability_enabled(True)`.
- **TaskExecutor._build_execution_components(session)** reads those flags and,
  per execution, builds the session-bound components on the **live execution
  session** — `BudgetManager(TaskRepository(session))` and
  `CheckpointManager(session)` — then hands them (plus the singletons) to the
  `TaskManager` it constructs.

This makes governance consistent across **every** entry point — the
`task.execute` module, REST/MCP/A2A, and the built-in scheduler — because they
all execute through the same `TaskExecutor`. The session-bound components are
never shared across sessions; they are rebuilt each execution so they share the
executor's transaction.

> Note: `BudgetManager` and `CheckpointManager` are async against the live
> `AsyncSession` (the execution session is always async). Earlier these classes
> were sync and only exercised by sync-mock unit tests, so the missing `await`
> went unnoticed until they were wired into the real execution path.

Coverage: `tests/governance/test_integration.py` proves the async `BudgetManager`
works against a real repository and that `TaskExecutor` assembles the components
when governance is enabled.

---

## Acceptance Criteria

1. A task with `token_budget=1000` is blocked when cumulative usage reaches the limit.
2. A task with `token_budget=1000` and action=DOWNGRADE passes the next model to its executor when utilization reaches the configured threshold (e.g. 800 tokens at 80%).
3. A task with `token_budget=1000` and cost policy action=NOTIFY logs a warning and continues execution.
4. Token usage reported by the executor's `execute()` return dict is accumulated once in the task's `token_usage` field for both successful and failed results.
5. Cost policies can be registered by name and evaluated against current utilization.
6. Model downgrade chains work with any string model identifier (provider-agnostic).
7. Usage reports aggregate token counts and costs across tasks for a given user and time period.
8. Usage reports can be exported as JSON.
9. All cost data persists in the database (token_usage, token_budget, estimated_cost_usd, actual_cost_usd, cost_policy columns).

Execution-level regression coverage: `tests/governance/test_execution_enforcement.py`
checks threshold boundaries, unknown policies, failed-result usage, and stored
application-supplied costs including zero amounts.
