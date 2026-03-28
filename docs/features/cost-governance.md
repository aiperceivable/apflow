# Feature Spec: Cost Governance (F-004)

**Feature ID:** F-004
**Priority:** P0
**Phase:** Phase 3 (0.20.0-beta.1)
**Tech Design Reference:** Section 4.5

---

## Purpose

Provide token budget management and cost policy enforcement for AI agent tasks. Enables per-task budgets with configurable actions (block, downgrade model, notify) when budgets are approached or exceeded. Token usage data comes from the executor's `execute()` return value, making this framework-agnostic.

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
2. In the single-task execution path (before executor creation):
   ```python
   # Pre-execution budget check
   if self._budget_manager:
       budget_check = await self._budget_manager.check_budget(task_id)
       if not budget_check.allowed:
           if self._policy_engine and task.cost_policy:
               evaluation = self._policy_engine.evaluate(
                   task.cost_policy,
                   budget_check.utilization,
               )
               if evaluation.action == PolicyAction.BLOCK:
                   # Fail task with budget exhausted error
                   await self.task_repository.update_task(
                       task_id=task_id, status="failed",
                       error=f"Budget exhausted: {evaluation.message}",
                   )
                   return
               elif evaluation.action == PolicyAction.DOWNGRADE and evaluation.model_override:
                   # Inject model override into inputs
                   task_inputs = task.inputs or {}
                   task_inputs["model"] = evaluation.model_override
               elif evaluation.action == PolicyAction.NOTIFY:
                   logger.warning(f"Budget warning for task {task_id}: {evaluation.message}")
           else:
               await self.task_repository.update_task(
                   task_id=task_id, status="failed",
                   error="Token budget exhausted and no cost policy configured.",
               )
               return
   ```

3. In `_handle_task_execution_result()` (after status update to completed):
   ```python
   # Post-execution budget update
   token_usage = task_result.get("token_usage")
   if token_usage and self._budget_manager:
       await self._budget_manager.update_usage(task_id, token_usage)
   ```

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
    scope: BudgetScope       # TASK or USER
    scope_id: str            # Non-empty string
    limit: int               # >= 1
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
        3. If task.token_budget is None: return allowed=True, remaining=-1 (unlimited).
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

## Data Models

### TaskModel Additions

| Field | SQLAlchemy Type | Python Type | Default | Validation |
|---|---|---|---|---|
| `token_usage` | `JSON` | `Optional[dict]` | `None` | `{input: int>=0, output: int>=0, total: int>=0}` |
| `token_budget` | `Integer` | `Optional[int]` | `None` | `>= 1` when set |
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

def test_token_budget_limit_zero_raises():
    """limit=0 raises ValueError."""

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

async def test_budget_manager_update_accumulates():
    """Two updates accumulate: first {total: 300}, second {total: 200} -> total=500."""

async def test_budget_manager_update_negative_raises():
    """Negative token_usage value raises ValueError."""
    with pytest.raises(ValueError, match=">= 0"):
        await bm.update_usage("t1", {"input": -1, "output": 0, "total": -1})

async def test_budget_manager_update_returns_budget():
    """Returns updated TokenBudget when budget is configured."""

async def test_budget_manager_update_returns_none_no_budget():
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
async def test_budget_enforcement_end_to_end():
    """Task with budget=1000 executes twice. First uses 600, second is blocked.
    Steps:
    1. Create task with token_budget=1000.
    2. Mock executor returns token_usage={total: 600}.
    3. Execute. Success. Budget remaining=400.
    4. Mock executor returns token_usage={total: 500}.
    5. Execute. Budget check fails. Task marked failed.
    """

async def test_downgrade_chain_end_to_end():
    """Task with budget=1000 and downgrade policy triggers model switch.
    Steps:
    1. Register policy: threshold=0.7, action=DOWNGRADE, chain=["opus","sonnet","haiku"].
    2. Create task with budget=1000, cost_policy="default".
    3. First execution: uses 800 tokens (utilization 0.8 > 0.7).
    4. Second execution: policy triggers downgrade. Verify model_override="sonnet" in inputs.
    """

async def test_token_usage_flow():
    """Executor result with token_usage is stored in task record.
    Steps:
    1. Create task.
    2. Execute with mock executor returning {output: "data", token_usage: {input: 100, output: 200, total: 300}}.
    3. Query task. Verify token_usage={input: 100, output: 200, total: 300}.
    """
```

---

## Acceptance Criteria

1. A task with `token_budget=1000` is blocked when cumulative usage reaches the limit.
2. A task with `token_budget=1000` and cost policy action=DOWNGRADE switches to the next model in the chain when utilization exceeds the threshold.
3. A task with `token_budget=1000` and cost policy action=NOTIFY logs a warning and continues execution.
4. Token usage reported by the executor's `execute()` return dict is accumulated in the task's `token_usage` field.
5. Cost policies can be registered by name and evaluated against current utilization.
6. Model downgrade chains work with any string model identifier (provider-agnostic).
7. Usage reports aggregate token counts and costs across tasks for a given user and time period.
8. Usage reports can be exported as JSON.
9. All cost data persists in the database (token_usage, token_budget, estimated_cost_usd, actual_cost_usd, cost_policy columns).
