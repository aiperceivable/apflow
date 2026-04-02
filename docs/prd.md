# apflow v2 — Product Requirements Document

> **Positioning Update:** The product has been repositioned from "AI Agent Production Middleware"
> to **"AI-Perceivable Distributed Orchestration"** (AP+Flow). apflow is a distributed task
> orchestration engine where every capability is AI-perceivable via the apcore module standard.
> This PRD retains original requirements and analysis. See README.md for current positioning.

**Product:** apflow v2 — AI-Perceivable Distributed Orchestration
**Version:** 0.20.0 (MVP)
**Author:** apflow team
**Date:** 2026-03-28
**Status:** Draft

---

## 1. Executive Summary

**One-line positioning:** apflow v2 is framework-agnostic production middleware that makes AI agents reliable, cost-governed, and auditable — without replacing your agent framework.

**The problem:** 79% of enterprises are adopting AI agents, but only 11% reach production. The gap is not in building agents — dozens of mature frameworks handle that — but in making them production-grade: durable execution, cost control, and governance.

**The solution:** apflow v2 sits between any agent framework and production infrastructure, providing durable execution (checkpoint/resume), cost governance (token budgets, model downgrade chains), and a unified module system via the apcore ecosystem. It does not compete with LangGraph, CrewAI, or any agent framework — it makes them production-ready.

**Key differentiator:** The apcore ecosystem (apcore, apcore-mcp, apcore-a2a, apcore-cli) — all stable, all available — gives apflow automatic protocol exposure (MCP, A2A) and CLI generation without building any of it from scratch. No competitor in the middleware space has this.

---

## 2. Problem Statement

### Current Pain Points

1. **The Demo-to-Production Gap.** Building an AI agent demo takes days. Getting it to production takes months. The missing pieces are not intelligence but reliability: what happens when an agent fails mid-execution? What happens when token costs spike? What happens when you need an audit trail?

2. **Framework Lock-in.** LangGraph offers checkpointing — but only for LangGraph agents. CrewAI has its own execution model. OpenAI Agents SDK has its own. There is no framework-agnostic durable execution layer.

3. **Cost Unpredictability.** Token budgets burn millions in enterprise settings. A single agentic loop with no cost guard can exhaust a monthly budget in hours. There is no mature, framework-agnostic cost governance solution.

4. **Protocol Fragmentation.** MCP (97M monthly SDK downloads) and A2A (100+ enterprise supporters) are both under the Linux Foundation. But integrating agents with these protocols requires significant custom wiring.

### Why Existing Solutions Don't Solve This

| Category | Examples | Gap |
|---|---|---|
| Agent Frameworks | LangGraph, CrewAI, OpenAI Agents SDK, Claude Agent SDK, Google ADK | Build agents, don't solve production reliability across frameworks |
| LLM Routing | LiteLLM, Portkey, Bifrost | Route API calls, don't manage agent execution lifecycle |
| Observability | Langfuse, LangSmith, AgentOps | Observe agents, don't control or recover them |
| Visual Orchestration | Dify, n8n, Flowise | Low-code builders, not middleware for existing code |
| Code Sandboxes | E2B, Modal, Daytona | Isolate execution, don't manage agent state or cost |

### What Happens If We Don't Build This

apflow v1 remains a task orchestration framework competing against massively funded platforms (Dify at 114k stars, n8n at 150k stars). It has no meaningful differentiation. The codebase carries ~17,500 lines of self-built protocol and CLI code that duplicates what apcore already does better.

---

## 3. Target Users

### Primary Audience

Developers who are building AI agents with any framework and need to move them to production.

### User Personas

**Persona 1: The AI Application Developer ("Alex")**
- Building agentic features in a product (customer support bot, code assistant, data pipeline)
- Uses LangGraph or CrewAI or OpenAI Agents SDK
- Needs: reliable execution (retries, checkpoints), cost control, easy integration
- Pain: agents fail silently, costs are unpredictable, no audit trail

**Persona 2: The AI Platform Engineer ("Jordan")**
- Responsible for the internal AI platform at a mid-size company
- Multiple teams use different agent frameworks
- Needs: unified governance, cost policies across teams, protocol compliance (MCP/A2A)
- Pain: each team reinvents reliability, no unified cost reporting

**Persona 3: The Independent Developer ("Sam")**
- Solo or small team building AI-powered tools
- Cannot afford to build production infrastructure from scratch
- Needs: lightweight middleware that "just works," quick protocol exposure via MCP/A2A
- Pain: wants to ship fast, not build infra

### Jobs to Be Done

1. "I want my agent to recover from failures without losing progress."
2. "I want to set a token budget and know it will be enforced."
3. "I want my existing agent code exposed via MCP and A2A without rewriting anything."
4. "I want to see how much each agent run costs and who triggered it."

---

## 4. Product Vision & Positioning

### Vision Statement

Make any AI agent production-ready — regardless of which framework built it — by providing the reliability, governance, and protocol layers that every agent needs but no framework provides.

### What apflow IS and IS NOT

| apflow IS | apflow IS NOT |
|---|---|
| Production middleware for AI agents | An agent framework (use LangGraph/CrewAI/etc.) |
| Framework-agnostic reliability layer | A replacement for your existing stack |
| Cost governance and budget enforcement | An LLM routing layer (use LiteLLM/Portkey) |
| A bridge to MCP and A2A protocols | An observability platform (use Langfuse) |
| Built on the apcore module standard | A visual workflow builder |

### Relationship to the apcore Ecosystem

```
+------------------------------------------------------------------+
|                        Protocol Layer                             |
|   apcore-mcp (MCP server)  |  apcore-a2a (A2A)  |  apcore-cli   |
+------------------------------------------------------------------+
|                        apcore (Module Standard)                   |
|   Registry  |  Executor  |  ACL  |  Middleware  |  Observability  |
+------------------------------------------------------------------+
|                        apflow v2 (This Product)                   |
|   Durable Execution  |  Cost Governance  |  Task Orchestration    |
+------------------------------------------------------------------+
|                        Agent Frameworks (Bring Your Own)          |
|   LangGraph  |  CrewAI  |  OpenAI SDK  |  Custom  |  Any         |
+------------------------------------------------------------------+
```

apflow v2 registers its capabilities as apcore Modules. The apcore ecosystem automatically exposes them via MCP, A2A, and CLI. apflow does not build or maintain any protocol servers.

### Competitive Differentiation

| Capability | apflow v2 | LangGraph | Temporal + LLM | Custom build |
|---|---|---|---|---|
| Framework-agnostic | Yes | No (LangGraph only) | Partial | N/A |
| Durable execution | Yes | Yes (LangGraph only) | Yes (complex setup) | DIY |
| Cost governance | Yes (built-in) | No | No | DIY |
| MCP/A2A protocol exposure | Yes (via apcore) | No | No | DIY |
| CLI auto-generation | Yes (via apcore-cli) | No | No | DIY |
| Setup complexity | pip install | pip install | Significant infra | Months |

---

## 5. Feature Requirements

### P0 — MVP (v0.20.0): Must Have

---

#### F-001: Project Slimming

**Description:** Remove all self-built protocol layers, CLI, and extensions that are superseded by the apcore ecosystem or out of scope for v2.

**What to delete (~17,561 lines, ~64 files):**

| Module | Lines | Files | Replacement |
|---|---|---|---|
| `api/a2a/` | ~1,840 | 7 | apcore-a2a |
| `api/mcp/` | ~933 | 8 | apcore-mcp |
| `api/graphql/` | ~646 | 9 | None (dropped) |
| `api/docs/` | ~727 | 3 | apcore-mcp Tool Explorer |
| `cli/` | ~6,979 | 18 | apcore-cli |
| `extensions/crewai/` | ~1,747 | 4 | Future: thin agent adapter |
| `extensions/llm/` | ~251 | 2 | Future: agent adapter |
| `extensions/generate/` | ~3,651 | 8 | Dropped (code generation) |
| `extensions/grpc/` | ~314 | 2 | Dropped |
| `extensions/tools/` | ~473 | 3 | Dropped |

**What to update:**
- `pyproject.toml`: remove deleted extras (`a2a`, `cli`, `graphql`, `crewai`, `llm`, `grpc`, `tools`, `standard`, `all`); add `apcore>=0.14.0`, `apcore-mcp>=0.10.1`, `apcore-a2a`, `apcore-cli>=0.3.0` as dependencies or extras
- `__init__.py`: update description to "AI Agent Production Middleware", version to `0.20.0`
- Remove `project.scripts` entries (`apflow`, `apflow-server`)

**Acceptance criteria:**
1. All listed directories are deleted from the source tree.
2. `pyproject.toml` has no references to deleted modules.
3. `pip install apflow` succeeds with only core + apcore dependencies.
4. Remaining test suite passes (tests for deleted modules are also removed).
5. Total source line count drops from ~48.5k to ~31k.

**Priority:** P0
**Estimated effort:** 3-5 days

---

#### F-002: apcore Module Bridge

**Description:** Register apflow's capabilities as apcore Modules so that apcore-mcp, apcore-a2a, and apcore-cli can automatically expose them.

**Requirements:**
1. Create `bridge/` module with apcore registration logic.
2. Adapt `ExecutableTask` to implement the apcore Module interface (schema-enforced input/output, metadata).
3. Register traditional executors as apcore Modules:
   - REST executor -> `apflow.rest` module
   - SSH executor -> `apflow.ssh` module
   - Docker executor -> `apflow.docker` module
   - Email executor -> `apflow.email` module
   - Scrape executor -> `apflow.scrape` module
   - WebSocket executor -> `apflow.websocket` module
   - MCP executor -> `apflow.mcp` module
   - Command/stdio executor -> `apflow.command` module
4. Register TaskRoutes handlers (create, execute, list, get, delete) as apcore Modules.
5. Initialize apcore Registry on apflow startup.

**Acceptance criteria:**
1. `apcore-mcp` can start and expose all registered modules as MCP tools.
2. `apcore-a2a` can start and expose registered modules as A2A skills.
3. `apcore-cli` can generate CLI commands for registered modules.
4. Each module has proper JSON Schema for inputs and outputs.
5. Module registration is declarative and requires no code changes in executors.

**Priority:** P0
**Estimated effort:** 5-8 days

---

#### F-003: Durable Agent Execution

**Description:** Enable checkpoint/resume for long-running agent tasks so that failures do not lose progress.

**Requirements:**

*TaskModel extensions:*
- `checkpoint_at: Optional[datetime]` — when the last checkpoint was saved
- `resume_from: Optional[str]` — checkpoint ID to resume from
- `attempt_count: int` — number of execution attempts (default 0)
- `max_attempts: int` — maximum retry attempts (default 3)
- `backoff_strategy: Optional[str]` — "fixed", "exponential", "linear" (default "exponential")
- `backoff_base_seconds: float` — base delay between retries (default 1.0)

*ExecutableTask interface extensions:*
- `supports_checkpoint() -> bool` — whether this task type supports checkpointing
- `get_checkpoint() -> Optional[dict]` — serialize current state
- `resume_from_checkpoint(checkpoint: dict) -> None` — restore state from checkpoint

*Checkpoint storage:*
- Checkpoints stored in a separate `task_checkpoints` table (not on TaskModel) — allows large checkpoints, independent lifecycle, and full checkpoint history
- Database migration 004 for new fields and table
- Checkpoint data is JSON-serializable; binary data stored as base64

*Retry logic:*
- On task failure, check `attempt_count < max_attempts`
- Apply backoff strategy to calculate delay
- Save checkpoint before retry if task supports it
- Resume from checkpoint on retry if available

*Circuit breaker:*
- Strengthen existing timeout capabilities
- Add `circuit_breaker_threshold: int` — consecutive failures before circuit opens (default 5)
- Add `circuit_breaker_reset_seconds: float` — time before circuit half-opens (default 60.0)

**Acceptance criteria:**
1. A task that fails mid-execution can be retried and resumes from its last checkpoint.
2. Retry backoff follows the configured strategy (fixed, exponential, linear).
3. Circuit breaker prevents repeated execution of consistently failing tasks.
4. Checkpoint data persists across process restarts.
5. Database migration 004 applies cleanly on both SQLite and PostgreSQL.
6. Tasks that do not support checkpointing still benefit from retry logic (they restart from the beginning).

**Priority:** P0
**Estimated effort:** 8-12 days

---

#### F-004: Cost Governance

**Description:** Provide token budget management and cost policy enforcement for AI agent tasks.

**Requirements:**

*TaskModel extensions:*
- `token_usage: Optional[dict]` — `{"input": int, "output": int, "total": int}`
- `token_budget: Optional[int]` — maximum tokens allowed for this task
- `estimated_cost_usd: Optional[float]` — pre-execution cost estimate
- `actual_cost_usd: Optional[float]` — post-execution actual cost
- `cost_policy: Optional[str]` — policy name to apply

*New module: `governance/`*

`governance/budget.py`:
- `TokenBudget` dataclass: `limit`, `used`, `remaining`, `scope` (task/user/global)
- `BudgetManager`: track usage, check limits, aggregate across scopes
- Per-task budgets checked before and during execution

`governance/policy.py`:
- `CostPolicy` dataclass: `name`, `action`, `threshold`, `downgrade_chain`
- Actions: `block` (reject task), `downgrade` (use cheaper model), `notify` (log warning, continue), `continue` (no action)
- `PolicyEngine`: evaluate policies against current usage

`governance/provider_router.py`:
- Model downgrade chains: configurable sequences (e.g., `claude-opus-4-20250514` -> `claude-sonnet-4-20250514` -> `claude-haiku-4-20250514`)
- When budget threshold is hit and policy is `downgrade`, automatically select next model in chain
- Provider-agnostic: works with any model identifier string

`governance/reporter.py`:
- Usage summary by task, user, time period
- Export as JSON for integration with external dashboards
- No built-in UI (use apcore-mcp Tool Explorer or external tools)

**Acceptance criteria:**
1. A task with `token_budget=1000` is blocked (or downgraded) when usage approaches the limit.
2. Cost policies can be defined in configuration and applied by name.
3. Model downgrade chains work: when budget triggers downgrade, the next model in the chain is used.
4. Token usage is recorded on every task that involves LLM calls.
5. Usage reports can be generated for any time period.
6. All cost data persists in the database.

**Priority:** P0
**Estimated effort:** 8-10 days

---

#### F-005: TaskCreator Relaxation

**Description:** Remove the single-root constraint from TaskCreator, allowing multi-root task forests.

**Requirements:**
- `parent_id` becomes truly optional (no default root creation)
- Multiple independent task trees can coexist
- All existing validation preserved: circular dependency detection, reference validation, duplicate ID detection
- No changes to TaskManager execution logic (it already handles independent tasks)

**Acceptance criteria:**
1. Tasks can be created without `parent_id` and exist as independent roots.
2. Multiple root tasks can coexist in the same session.
3. Circular dependency detection still works correctly.
4. Existing task creation patterns (with parent_id) continue to work unchanged.

**Priority:** P0
**Estimated effort:** 1-2 days

---

### P1 — v0.21.0: Should Have

---

#### F-006: Agent Adapters

**Description:** Provide thin adapter layers that wrap popular agent frameworks as apflow executable tasks.

**Requirements:**
- `AgentAdapter` Protocol: `async execute(input: dict) -> dict`, `supports_checkpoint() -> bool`, `get_checkpoint() -> Optional[dict]`
- `GenericAdapter`: wraps any `async callable` as an agent adapter
- `LangGraphAdapter`: wraps a LangGraph compiled graph, maps LangGraph checkpoints to apflow checkpoints
- `CrewAIAdapter`: wraps a CrewAI crew (thin, not the old heavy extension)
- `OpenAIAgentsAdapter`: wraps OpenAI Agents SDK runner

**Acceptance criteria:**
1. Each adapter can execute its target framework's agent as an apflow task.
2. LangGraph adapter preserves LangGraph's native checkpoint data.
3. All adapters are optional dependencies (not required for core apflow).
4. Each adapter is <300 lines of code.

**Priority:** P1
**Estimated effort:** 5-8 days per adapter

---

#### F-007: TaskModel Agent Extensions

**Description:** Add agent-specific metadata fields to TaskModel.

**Requirements:**
- `agent_framework: Optional[str]` — which framework produced this task ("langgraph", "crewai", "openai", etc.)
- `agent_model: Optional[str]` — the LLM model used
- `agent_config: Optional[dict]` — framework-specific configuration

**Acceptance criteria:**
1. Fields are optional and do not affect existing task behavior.
2. Database migration applies cleanly.
3. Fields are exposed via apcore module metadata.

**Priority:** P1
**Estimated effort:** 1-2 days

---

#### F-008: Observability Integration

**Description:** Integrate with Langfuse and OpenTelemetry for cost and usage reporting.

**Requirements:**
- Langfuse integration: export cost/usage data from governance module to Langfuse traces
- OpenTelemetry: export execution spans with task metadata
- Both integrations are optional dependencies

**Acceptance criteria:**
1. Langfuse receives cost data for every LLM-involving task execution.
2. OpenTelemetry spans include task ID, execution time, token usage, and cost.
3. Neither integration is required for apflow to function.

**Priority:** P1
**Estimated effort:** 3-5 days

---

### P2 — Future: Nice to Have

---

#### F-009: Advanced Cost Strategies

**Description:** Predictive cost analysis and cross-provider comparison.

**Requirements:**
- Pre-execution cost prediction based on input size and model pricing
- Cross-provider cost comparison (same prompt, different providers)
- Historical cost analytics with trend detection

**Acceptance criteria:**
1. Cost predictions are within 20% of actual cost for standard workloads.
2. Historical analytics are queryable by time range, user, and task type.

**Priority:** P2
**Estimated effort:** 5-8 days

---

#### F-010: Multi-Agent Coordination

**Description:** Orchestrate agents across frameworks using A2A protocol.

**Requirements:**
- Cross-framework agent orchestration: a LangGraph agent can delegate to a CrewAI agent via A2A
- Agent result aggregation: combine outputs from multiple agents
- Verification: cross-check agent outputs for consistency

**Acceptance criteria:**
1. Two agents from different frameworks can collaborate on a task via A2A.
2. Results from multiple agents can be aggregated into a single output.

**Priority:** P2
**Estimated effort:** 10-15 days

---

## 6. Technical Architecture

### Architecture Layers

```
Layer 4: Protocol Exposure (apcore ecosystem — NOT built by apflow)
  ┌─────────────┬─────────────┬─────────────┐
  │ apcore-mcp  │ apcore-a2a  │ apcore-cli  │
  └─────────────┴─────────────┴─────────────┘

Layer 3: Module Standard (apcore — NOT built by apflow)
  ┌─────────────────────────────────────────────┐
  │ Registry │ Executor │ ACL │ Middleware │ Obs │
  └─────────────────────────────────────────────┘

Layer 2: apflow v2 (THIS PRODUCT)
  ┌──────────────┬──────────────┬───────────────┐
  │   Durable    │     Cost     │    Module      │
  │  Execution   │  Governance  │    Bridge      │
  ├──────────────┴──────────────┴───────────────┤
  │         Task Orchestration Engine            │
  │  TaskManager │ TaskCreator │ TaskExecutor    │
  ├─────────────────────────────────────────────┤
  │         Executors (REST, SSH, Docker, ...)   │
  ├─────────────────────────────────────────────┤
  │         Storage (SQLite / PostgreSQL)         │
  └─────────────────────────────────────────────┘

Layer 1: Agent Frameworks (USER'S CHOICE — not built by apflow)
  ┌───────────┬─────────┬──────────┬────────────┐
  │ LangGraph │ CrewAI  │ OpenAI   │ Any async  │
  │           │         │ Agents   │ callable   │
  └───────────┴─────────┴──────────┴────────────┘
```

### Code Inventory

**What is deleted (~17,561 lines):**

| Module | Lines | Reason |
|---|---|---|
| `api/a2a/` | 1,840 | Replaced by apcore-a2a |
| `api/mcp/` | 933 | Replaced by apcore-mcp |
| `api/graphql/` | 646 | Dropped |
| `api/docs/` | 727 | Replaced by apcore-mcp Tool Explorer |
| `cli/` | 6,979 | Replaced by apcore-cli |
| `extensions/crewai/` | 1,747 | Replaced by thin adapter (P1) |
| `extensions/llm/` | 251 | Replaced by agent adapter (P1) |
| `extensions/generate/` | 3,651 | Dropped (code generation out of scope) |
| `extensions/grpc/` | 314 | Dropped |
| `extensions/tools/` | 473 | Dropped |

**What is preserved (~30,965 lines):**

| Module | Lines | Notes |
|---|---|---|
| `core/` | 18,974 | Task engine, models, executors, distributed runtime — the heart of apflow |
| `scheduler/` | 2,035 | Task scheduling |
| `extensions/ssh/` | 342 | SSH executor |
| `extensions/docker/` | 394 | Docker executor |
| `extensions/email/` | 425 | Email executor |
| `extensions/scrape/` | 117 | Web scrape executor |
| `extensions/websocket/` | 277 | WebSocket executor |
| `extensions/mcp/` | 523 | MCP client executor (not server) |
| `extensions/http/` | 340 | HTTP/REST executor |
| `extensions/stdio/` | 566 | Stdio executor |
| `extensions/apflow/` | 651 | Internal executors |
| `extensions/core/` | 234 | Core extension utilities |
| `extensions/hooks/` | 153 | Hook system |
| `extensions/llm_key_config/` | 158 | LLM key management |
| `extensions/storage/` | 210 | Storage extension |
| Other files | ~5,566 | `__init__.py`, `logger.py`, etc. |

**What is new (estimated ~3,000-5,000 lines):**

| Module | Est. Lines | Description |
|---|---|---|
| `bridge/` | 500-800 | apcore Module registration |
| `governance/` | 800-1,200 | Budget, policy, provider router, reporter |
| `durability/` | 600-1,000 | Checkpoint, retry, circuit breaker logic |
| TaskModel extensions | 200-300 | New fields and migration |
| Tests | 1,000-2,000 | Unit tests for new modules |

### Dependency Changes

**Removed from `pyproject.toml`:**
- `a2a-sdk`, `fastapi`, `uvicorn`, `starlette`, `websockets` (from a2a extra)
- `click`, `rich`, `typer`, `python-dotenv`, `nest_asyncio` (from cli extra)
- `strawberry-graphql` (from graphql extra)
- `crewai`, `litellm`, `anthropic` (from crewai extra)
- `grpclib`, `protobuf` (from grpc extra)

**Added to `pyproject.toml`:**
- `apcore>=0.14.0` (core dependency)
- `apcore-mcp>=0.10.1` (optional: MCP exposure)
- `apcore-a2a` (optional: A2A exposure)
- `apcore-cli>=0.3.0` (optional: CLI generation)

---

## 7. Non-Functional Requirements

| Requirement | Target | Measurement |
|---|---|---|
| Execution overhead | <50ms per task dispatch | Benchmark: 1000 no-op tasks |
| Checkpoint recovery | >95% success rate | Test: kill/resume 100 checkpointed tasks |
| Schema migrations | Backward compatible | Migration runs on existing v1 databases without data loss |
| Python version | >=3.11 | Aligned with apcore requirement |
| Test coverage | >=90% on new code | `pytest --cov` on `bridge/`, `governance/`, `durability/` |
| Existing test adaptation | All preserved modules pass | Adapted tests for deleted imports/dependencies |
| Package size | Core install <5MB | `pip install apflow` without optional extras |

---

## 8. Risks & Mitigations

### Technical Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| apcore Module interface does not fit all executor patterns | Medium | High | Prototype bridge with 2-3 executors first; negotiate interface changes with apcore if needed |
| Checkpoint serialization complexity (non-JSON-serializable state) | Medium | Medium | Restrict checkpoints to JSON-serializable data; provide base64 escape hatch for binary |
| SQLite migration compatibility | Low | High | Test migration 004 on both SQLite and PostgreSQL before release |
| Performance regression from apcore middleware pipeline | Low | Medium | Benchmark with and without apcore; bypass middleware for internal execution paths if needed |

### Product Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| "Production middleware" positioning is too abstract for developers | Medium | High | Lead with concrete use cases: "add retries and cost limits to your LangGraph agent in 5 minutes" |
| Market moves to all-in-one platforms (LangGraph adds cost governance) | Medium | High | Stay framework-agnostic; the value is in not being locked in |
| apcore ecosystem has limited adoption, reducing network effects | Medium | Medium | Dogfood first; don't depend on external apcore adoption for v2 to be useful |

### Resource Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| 2-3 person team cannot deliver all P0 features | Medium | High | Strict phased delivery (alpha.1 -> alpha.2 -> beta.1); each phase is independently useful |
| Maintaining both v1 branch and v2 | Low | Low | v1 branch is frozen; no maintenance commitment |

---

## 9. Success Metrics

### Phase 1: Dogfood (0.20.0 release)

- apflow v2 successfully runs its own internal workflows with durable execution
- Cost governance tracks and enforces budgets on real LLM tasks
- apcore-mcp and apcore-a2a expose apflow modules without custom protocol code
- Zero self-built protocol/CLI code remains in the codebase

### Phase 2: Community (0.21.0+)

- GitHub stars growth rate (baseline: current count at v2 launch)
- PyPI monthly downloads (baseline: current at v2 launch)
- At least 2 agent framework adapters validated by external users

### Phase 3: Ecosystem (1.0.0)

- 3+ agent frameworks with validated adapters
- External contributors submitting adapters or governance policies
- Documented production deployments (even if small scale)

*Note: Specific numeric targets are not set because this is an early-stage project. Growth rates matter more than absolute numbers.*

---

## 10. Version Roadmap

| Version | Codename | Scope | Features | Est. Duration |
|---|---|---|---|---|
| **0.20.0-alpha.1** | Phase 1: Slim & Bridge | Storage migration + slimming + apcore bridge | F-SM, F-001, F-002, F-005 | 2-3 weeks |
| **0.20.0-alpha.2** | Phase 2: Durability | Durable execution | F-003 | 2-3 weeks |
| **0.20.0-beta.1** | Phase 3: Governance | Cost governance | F-004 | 2-3 weeks |
| **0.20.0** | MVP Release | Stabilization, docs, dogfood | Bug fixes, documentation | 1-2 weeks |
| **0.21.0** | Agent Adapters | Framework integration | F-006, F-007, F-008 | 4-6 weeks |
| **1.0.0** | Production-Ready | Stability, advanced features | F-009, F-010, hardening | TBD |

**Total estimated MVP timeline: 7-11 weeks** (no hard deadline; quality first)

---

## 11. Open Questions

1. ~~**Checkpoint storage format:**~~ **RESOLVED.** Checkpoints use a dedicated `task_checkpoints` table. A dedicated table allows larger checkpoints, independent cleanup, and full checkpoint history. See tech-design Section 4.4 and feature spec `durable-execution.md`.

2. ~~**Cost data source:**~~ **RESOLVED.** Executors report token usage via the `execute()` return dict (`token_usage: {input, output, total}`). This is framework-agnostic and requires no LiteLLM dependency. See tech-design Section 4.5 and feature spec `cost-governance.md`.

3. ~~**apcore Module granularity:**~~ **RESOLVED.** Each executor is a separate apcore Module (e.g., `apflow.rest_executor`, `apflow.ssh_executor`). Task management operations are also separate modules (`apflow.task.create`, etc.). See tech-design Section 4.3 and feature spec `apcore-bridge.md`.

4. **Budget scope hierarchy:** Should per-task budgets roll up into per-user budgets automatically, or are they independent? Hierarchical budgets add complexity but are more useful for platform teams. *Note: MVP implements per-task budgets only. Per-user aggregation is available via `UsageReporter` but not enforced as a budget.*

5. ~~**Python version floor:**~~ **RESOLVED.** apflow v2 requires `>=3.11`, aligned with apcore. This is a breaking change from v1's `>=3.10`, accepted for v2. See feature spec `project-slimming.md`.

6. ~~**Distributed runtime in v2:**~~ **RESOLVED.** Checkpoints are stored in the shared database. When a worker fails, another worker loads the checkpoint via the existing lease mechanism. No changes to leader election or task leasing. See tech-design Section 4.4 "Interaction with Distributed Runtime".

7. **Test migration strategy:** ~185 test files exist for v1. How many can be adapted vs. need rewriting? Should we establish a test baseline before slimming?

---

## 12. Appendix

### A. Market Research Data

| Statistic | Source Context |
|---|---|
| 79% enterprise AI agent adoption | Market landscape analysis |
| 11% of AI agents in production | Market landscape analysis |
| 97M monthly MCP SDK downloads | MCP ecosystem data |
| 100+ enterprise A2A supporters | A2A protocol data |
| Dify: 114k GitHub stars | Open source project data |
| n8n: 150k GitHub stars | Open source project data |
| Langfuse: 6M+ SDK installs/month | Open source project data |

### B. Competitive Landscape

| Player | Category | Strengths | Weakness (for our use case) |
|---|---|---|---|
| LangGraph | Agent framework | Mature, checkpointing, large community | Ecosystem-locked, no cost governance |
| CrewAI | Agent framework | Easy multi-agent, good DX | No durable execution, no cost control |
| OpenAI Agents SDK | Agent framework | OpenAI integration, handoffs | Vendor-locked, no checkpointing |
| Temporal | Workflow engine | Battle-tested durability | Not agent-aware, complex setup, no cost governance |
| Dify | Visual orchestration | 114k stars, visual builder | Not middleware, not for existing code |
| Langfuse | Observability | 6M installs, MIT license | Observe only, no control or recovery |
| LiteLLM | LLM routing | Wide provider support | Routing only, no execution management |

### C. Code Inventory Summary

| Category | Lines | Files | Percentage |
|---|---|---|---|
| Deleted (protocol/CLI/extensions) | ~17,561 | ~64 | 36% of codebase |
| Preserved (core/scheduler/executors) | ~30,965 | ~121 | 64% of codebase |
| New (bridge/governance/durability) | ~3,000-5,000 (est.) | ~15-25 (est.) | New code |
| **Post-MVP estimated total** | **~34,000-36,000** | **~140-150** | — |

### D. Feature Summary by Priority

| Priority | Count | Features |
|---|---|---|
| P0 (MVP) | 6 | F-SM (Storage Migration), F-001 through F-005 |
| P1 (0.21.0) | 3 | F-006 through F-008 |
| P2 (Future) | 2 | F-009, F-010 |
| **Total** | **11** | — |

*Note: F-SM (DuckDB-to-SQLite Storage Migration) was decomposed from F-001 during technical design as a separate deliverable. See `docs/features/storage-migration.md`.*
