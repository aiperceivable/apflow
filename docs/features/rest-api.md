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

---

## Scheduling Modules (`schedule.*`)

The task-scheduling lifecycle is exposed as apcore modules, so it surfaces on every protocol
(CLI/REST/MCP/A2A) and in the OpenAPI doc:

| Module | Purpose |
|---|---|
| `schedule.set` | Configure a task's schedule (type + expression) and compute its next run |
| `schedule.due` | List scheduled tasks whose next run time has arrived |
| `schedule.complete` | Record a scheduled run complete and advance the next run |
| `schedule.export_ical` | Export scheduled tasks as an iCalendar (`.ics`) feed |

These complement the existing `task.scheduled` module (which lists configured schedules).
`schedule.set` validates the schedule expression **before** persisting it (no partial write
on an invalid expression) and distinguishes a missing task from an operation failure.

The long-running scheduler daemon is intentionally **not** a module — it is a worker-style
lifecycle process, not a request/response call.

---

## Tests

- `tests/api/test_rest_adapter.py` — descriptors, execution, OpenAPI shape, error envelopes,
  SSE (streaming + non-streaming fallback).
- `tests/api/test_combined.py` — mount assembly and `_serve_all_async` wiring.
- `tests/bridge/test_schedule_modules.py` — the four `schedule.*` modules, including the
  invalid-schedule and missing-vs-failed paths.
- `tests/bridge/test_task_modules.py` — `TaskExecuteModule.stream()` progress relay,
  detached-producer drain, and disconnect cancellation.
