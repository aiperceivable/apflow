# Feature Spec: REST / HTTP API (F-012)

**Feature ID:** F-012
**Priority:** P1
**Phase:** Phase 4 (0.22.0)
**Tech Design Reference:** [apcore Bridge (F-002)](apcore-bridge.md)

---

## Purpose

Give apflow a plain REST/HTTP face over the apcore Registry — the missing sibling of
`apcore-mcp` and `apcore-a2a`. apcore already exposes the Registry over MCP, A2A, and the
CLI; this feature adds REST/JSON + OpenAPI so conventional HTTP clients (and an
auto-generated typed frontend client) can drive apflow without speaking a protocol.

A capability is registered **once** in the bridge Registry and is then reachable over REST,
MCP, A2A, and the CLI simultaneously. The adapter is intentionally registry-driven, so new
modules appear on every surface with no per-protocol wiring.

Design constraints:

- **No FastAPI dependency.** Built on Starlette + uvicorn, which are already transitive via
  `apcore-a2a` / `apcore-mcp`. OpenAPI 3.1 and a Swagger UI are generated directly from each
  module's input/output JSON Schema.
- **Same execution path as MCP/A2A.** Requests run through `Executor.call_async` /
  `Executor.stream`, so middleware, observability, ACL, and approval behave identically.
- **Extractable.** The adapter is self-contained and could later move to a standalone
  `apcore-rest` package mirroring `apcore_mcp.serve(registry)`.

> This is **not** a restoration of the deleted v1 `api/` layer (which hand-rolled its own
> MCP/A2A/GraphQL servers). It is a single thin REST face over the v2 apcore Registry.

---

## File Changes

### New Files

**`src/apflow/api/__init__.py`** — re-exports `build_rest_app`, `serve_rest`, `build_openapi`,
`assemble_combined`, `serve_all`.

**`src/apflow/api/rest.py`** — `build_rest_app(registry)` builds the Starlette app;
`serve_rest(registry, …)` runs it under uvicorn; `build_openapi(registry, …)` generates the
OpenAPI 3.1 document. Error→HTTP-status mapping follows apcore `ModuleError.code`.

**`src/apflow/api/combined.py`** — `assemble_combined(rest_app, a2a_app, mcp_app)` and
`serve_all(registry, …)` for the unified REST + A2A + MCP server.

**`src/apflow/api/auth.py`** — shared JWT wiring: `build_mcp_authenticator` /
`build_a2a_authenticator` (from `api.jwt_*` config), `resolve_verification_key`
(HS256 secret vs RS256 public key), and `apply_rest_auth` (wraps the Starlette app
with apcore-mcp's `AuthMiddleware`). One token validates on REST, A2A, and MCP.

**`src/apflow/api/webhook.py`** — `build_webhook_routes(…)`: the inbound
`POST /webhook/trigger/{task_id}` endpoint for external schedulers, injected via
`build_rest_app(extra_routes=…)` and guarded by its own HMAC/IP/rate-limit.

---

## REST Endpoints

Start with `apflow rest` (default `:8080`).

| Method & path | Description |
|---|---|
| `GET /` | Service info (name, version, module count, doc links) |
| `GET /healthz` | Liveness probe (`{"status": "ok"}`) |
| `GET /modules` | List every module descriptor (id, description, input/output schema, tags) |
| `GET /modules/{id}` | Single module descriptor |
| `POST /modules/{id}` | Execute a module with a JSON-object body (the module `inputs`) |
| `GET /openapi.json` | Generated OpenAPI 3.1 document |
| `GET /docs` | Swagger UI |
| `POST /webhook/trigger/{task_id}` | Trigger a scheduled task (opt-in via `--webhook`; see [Inbound Webhook](#inbound-webhook)) |

Module ids are dotted (e.g. `POST /modules/task.create`, `POST /modules/schedule.set`).
On error, the response body is `{"error": {"code": "...", "message": "..."}}` and the HTTP
status is mapped from the apcore error code (e.g. `MODULE_NOT_FOUND` → 404,
`SCHEMA_VALIDATION_ERROR` → 422, `MODULE_TIMEOUT` → 504).

```bash
# discover capabilities
curl localhost:8080/modules

# execute a module
curl -X POST localhost:8080/modules/task.create \
  -H 'content-type: application/json' \
  -d '{"name": "build"}'
```

Because the OpenAPI document is generated from the registry, a typed client can be generated
straight from it (e.g. `npx openapi-typescript http://localhost:8080/openapi.json`).

---

## SSE Streaming

`POST /modules/{id}` performs content negotiation: when the request `Accept` header contains
`text/event-stream`, the response is a Server-Sent Events stream of execution events instead
of a single JSON body.

- Streaming-capable modules emit one `data:` frame per chunk.
- Non-streaming modules emit a single `data:` frame (the final result).
- A terminal `event: done` always closes the stream; errors arrive as `event: error` rather
  than corrupting the SSE framing.

`task.execute` is a **streaming module**: it relays the engine's progress events
(`task_start` / `progress` / `task_completed` / `final`) as they occur, then a terminal
`{"type": "result", …}` event — so a UI can show live execution progress.

```bash
curl -N -X POST localhost:8080/modules/task.execute \
  -H 'accept: text/event-stream' \
  -H 'content-type: application/json' \
  -d '{"task_id": "<id>"}'
```

---

## Unified Server (`apflow serve --all`)

`apflow serve --all` mounts all three protocol surfaces on one port/process:

| Path | Surface |
|---|---|
| `/`, `/docs`, `/openapi.json`, `/modules*` | REST (this feature) |
| `/a2a/*` | A2A agent (`apcore-a2a`) |
| `/mcp`, `/health`, `/metrics` | MCP streamable-http (`apcore-mcp`) |

apcore-mcp hardcodes its endpoint at `/mcp`, so the MCP app is mounted as the root
catch-all and the lightweight REST routes are lifted ahead of it — keeping `/mcp` clean
while REST owns `/`, `/docs`, and `/modules`. uvicorn runs **inside** the
`apcore_mcp.async_serve` async context so the MCP transport's session-manager lifetime spans
the server (the documented embedding pattern).

The dedicated `apflow rest` (REST only) and `apflow serve` (A2A only) / `apflow mcp` (MCP
only) commands remain available for single-protocol deployments.

Cross-cutting flags: `--auth` (JWT — see below), `--webhook` / `--webhook-secret` (inbound
webhook), `--push-notifications` (A2A out-bound push: the server POSTs task results to a
client-supplied webhook, a standard A2A capability provided by `apcore-a2a`), and `--scheduler`
(run the built-in scheduler poll loop in-process — see [Triggering due tasks](#triggering-due-tasks)).
On `serve --all`, `--auth` protects all three surfaces at once.

---

## Authentication (JWT)

By default the HTTP surfaces are unauthenticated (suitable for trusted networks / local dev).
Pass `--auth` to require a JWT **Bearer** token. The same token validates on **REST, A2A, and
MCP** — REST reuses apcore-mcp's `AuthMiddleware`, A2A and MCP use their SDK's
`JWTAuthenticator` — all built from the shared `api.jwt_*` config, so one token works
everywhere.

`--auth` is opt-in and fails fast: if no verification key is configured, the command errors
before starting rather than running unauthenticated.

| Command | Effect of `--auth` |
|---|---|
| `apflow rest --auth` | REST module calls require a Bearer token |
| `apflow serve --auth` | A2A requests require a Bearer token |
| `apflow mcp --auth` | MCP HTTP transports require a Bearer token (`require_auth`) |
| `apflow serve --all --auth` | REST `/modules/*`, A2A, and MCP all require it |

**Algorithms — HS256 and RS256.** Set `api.jwt_algorithm` (default `HS256`; comma-separate for
multiple):

- **HS256/384/512 (symmetric):** verifies with `api.jwt_secret`.
- **RS256 / ES* / PS* (asymmetric):** verifies with a **public key** — `api.jwt_public_key`
  (inline PEM) or `api.jwt_public_key_path` (file). The private key signs tokens elsewhere and
  is never needed by the servers.

Optional `api.jwt_audience` / `api.jwt_issuer` enforce the `aud` / `iss` claims. See
[Environment Variables → Authentication](../guides/environment-variables.md#authentication-jwt).

**Exempt paths.** Public metadata stays reachable without a token: `/`, `/healthz`, `/docs`,
`/openapi.json` (and, on A2A, the agent-card discovery path). The inbound webhook is exempt
from JWT because it carries its own HMAC auth. On `serve --all`, the `/a2a` and `/mcp` prefixes
are exempt at the parent because those sub-apps authenticate themselves.

```bash
# HS256
export APFLOW_API_JWT_SECRET='a-long-random-secret-at-least-32-bytes'
apflow rest --auth
curl -H "Authorization: Bearer $TOKEN" localhost:8080/modules/task.list -d '{}'

# RS256 (verify with public key; sign elsewhere with the private key)
export APFLOW_API_JWT_ALGORITHM=RS256
export APFLOW_API_JWT_PUBLIC_KEY_PATH=/etc/apflow/jwt_public.pem
apflow serve --all --auth
```

> Use an HS256 secret of **≥32 bytes** in production (shorter keys trigger a PyJWT
> `InsecureKeyLengthWarning`). RS256 sidesteps shared-secret distribution entirely.

---

## Scheduling Modules (`schedule.*`)

The task-scheduling lifecycle is exposed as apcore modules, so it surfaces on every protocol
(CLI/REST/MCP/A2A) and in the OpenAPI doc:

| Module | Purpose |
|---|---|
| `schedule.set` | Configure a task's schedule (type + expression) and compute its next run |
| `schedule.due` | List scheduled tasks whose next run time has arrived |
| `schedule.trigger` | Execute a scheduled task now (registry-native inbound trigger) |
| `schedule.complete` | Record a scheduled run complete and advance the next run |
| `schedule.export_ical` | Export scheduled tasks as an iCalendar (`.ics`) feed |

These complement the existing `task.scheduled` module (which lists configured schedules).
`schedule.set` validates the schedule expression **before** persisting it (no partial write
on an invalid expression) and distinguishes a missing task from an operation failure.

### Triggering due tasks

Setting a schedule does not by itself run anything — something must poll for due tasks and
execute them. apflow offers two complementary triggers, both executing **in-process against
the database** (the built-in scheduler has no API/RPC mode):

- **Pull (built-in scheduler):** one poll loop calls `get_due_scheduled_tasks`, executes each
  due task, then advances the next run. Run it two ways:
  - **In-process with a server** (recommended for single-node): `apflow serve --all --scheduler`
    or `apflow rest --scheduler` runs the loop inside the server's event loop, so it shares the
    single SQLite writer — no second process contends for the database.
  - **Standalone process:** `apflow scheduler` (foreground) with `--poll-interval` (default
    60s), `--max-concurrent` (default 10), `--user-id`, `--task-timeout`, `--db`, `--verbose`.
- **Push (external scheduler):** cron / Kubernetes CronJob / Temporal trigger a task by URL or
  registry call — either `POST /webhook/trigger/{task_id}` (see [Inbound Webhook](#inbound-webhook))
  or the `schedule.trigger` module. Both reuse `WebhookGateway` (mark running → execute tree →
  advance next run).

**Consistency model.** Re-execution is prevented by `mark_scheduled_task_running` (atomic
status transition) plus the in-process `_active_task_ids` set, so a single poll loop never
double-runs a task. For **multi-node clusters** use `apflow worker` instead — its distributed
runtime leases tasks atomically (leader election + PostgreSQL row locks), which is the
supported path for distributed coordination. (The old v1 "route scheduler ops through the API
server for locking" mode has been removed; that indirection is unnecessary now that single-node
runs in-process and clusters use the worker lease.)

---

## Inbound Webhook

`POST /webhook/trigger/{task_id}` lets an external scheduler (cron, Kubernetes CronJob,
Temporal, …) push-trigger a scheduled task by a stable URL — the operational sibling of the
`schedule.trigger` module. It is **opt-in** (`apflow rest --webhook`, or `serve --all
--webhook`) and kept out of the registry-driven `rest.py` so that adapter stays a pure module
face; it is injected via `build_rest_app(extra_routes=…)`.

It authenticates standalone (the REST surface carries no JWT for it — and JWT auth, when on,
exempts the webhook path):

- **HMAC-SHA256** signature via `--webhook-secret`. Signed requests send
  `X-Webhook-Signature` (and optionally `X-Webhook-Timestamp`); an empty body is **still**
  signed (the `task_id` is in the URL), so the guard can't be bypassed by omitting the body.
- Optional IP allowlist and per-IP rate limit (via `WebhookConfig`).

Status mapping: a missing task → `404`; a task that ran and failed → `200` with
`{"success": false, …}` (a valid result external schedulers read from the body, not a transport
error). `?async=false` waits for the result instead of returning immediately.

```bash
apflow rest --webhook --webhook-secret "$SECRET"

# fire-and-forget trigger from cron
curl -X POST localhost:8080/webhook/trigger/<task_id> \
  -H "X-Webhook-Signature: $(printf '' | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"
```

`generate_cron_config` / `generate_kubernetes_cronjob` (in `apflow.scheduler.gateway.webhook`)
emit ready-to-use crontab lines and CronJob manifests pointing at this endpoint.

---

## Tests

- `tests/api/test_rest_adapter.py` — descriptors, execution, OpenAPI shape, error envelopes,
  SSE (streaming + non-streaming fallback).
- `tests/api/test_combined.py` — mount assembly, `_serve_all_async` wiring, push-notification
  and webhook threading, combined JWT auth threading, and in-process scheduler start/stop.
- `tests/scheduler/test_scheduler.py` — the in-process poll loop, due-task handling, and the
  `mark_scheduled_task_running` dedup (the removed v1 API mode leaves no `_use_api`/`_rpc_call`).
- `tests/api/test_auth.py` — REST `AuthMiddleware` gating (401/200), exempt paths, webhook
  exemption, HS256 vs RS256 key resolution, and an RS256 sign/verify round-trip.
- `tests/api/test_webhook.py` — `POST /webhook/trigger/{task_id}` routing, 404/200 status
  mapping, HMAC rejection, and `?async` override.
- `tests/cli/test_scheduler_command.py` — `apflow scheduler` option→config mapping and `--db`
  pool binding.
- `tests/cli/test_cli_bootstrap.py` — `--auth` guards (rest/serve/mcp error without a key).
- `tests/bridge/test_schedule_modules.py` — the `schedule.*` modules (set/due/trigger/complete/
  export_ical), including the invalid-schedule and missing-vs-failed paths.
- `tests/bridge/test_task_modules.py` — `TaskExecuteModule.stream()` progress relay,
  detached-producer drain, and disconnect cancellation.
