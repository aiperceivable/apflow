"""
Registry -> REST/HTTP adapter for apflow.

Exposes every apcore Registry module as a plain REST/JSON endpoint, mirroring how
``apcore_mcp.serve(registry)`` and ``apcore_a2a.serve(registry)`` expose the same
registry over MCP and A2A. This is the missing "Registry -> REST" sibling of those
protocol adapters: a capability registered once becomes reachable over REST, MCP,
A2A, and the CLI simultaneously.

Design notes:
- Built on Starlette + uvicorn (already transitive dependencies via apcore-a2a /
  apcore-mcp) -- deliberately NO FastAPI dependency. OpenAPI 3.1 and a Swagger UI
  are generated directly from each module's input/output JSON Schema, so the
  service is self-documenting without extra packages.
- Execution goes through ``Executor.call_async`` so REST requests run the exact
  same apcore pipeline (middleware, observability, ACL, approval) as MCP/A2A.
- Intentionally self-contained and registry-driven so it can later be extracted
  into a standalone ``apcore-rest`` package.

This is NOT a restoration of the deleted v1 ``api/`` layer (which hand-rolled its
own MCP/A2A/GraphQL servers and duplicated apcore). It is a single thin
registry-driven REST face over the v2 apcore Registry.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from apcore.errors import ModuleError
from apcore.executor import Executor
from apcore.registry.registry import Registry
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from apflow.logger import get_logger

logger = get_logger(__name__)

# HTTP status for known apcore error codes. Unlisted codes fall back to 400 for
# caller-fixable errors and 500 otherwise (see _status_for_error).
_HTTP_STATUS_BY_CODE: dict[str, int] = {
    "MODULE_NOT_FOUND": 404,
    "MODULE_DISABLED": 409,
    "MODULE_TIMEOUT": 504,
    "CIRCUIT_BREAKER_OPEN": 503,
    "ACL_DENIED": 403,
    "APPROVAL_DENIED": 403,
    "APPROVAL_PENDING": 202,
    "SCHEMA_VALIDATION_ERROR": 422,
    "SCHEMA_VALIDATION_FAILED": 422,
    "INVALID_INPUT": 422,
}


def _json_safe(value: Any) -> Any:
    """Convert common Python values to JSON-safe structures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump(mode="json"))
        except TypeError:
            return _json_safe(model_dump())

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict())

    return str(value)


def _status_for_error(exc: ModuleError) -> int:
    """Map an apcore ModuleError to an HTTP status code."""
    status = _HTTP_STATUS_BY_CODE.get(exc.code)
    if status is not None:
        return status
    # Caller-fixable errors are client-side (4xx); everything else is server-side.
    return 400 if getattr(exc, "user_fixable", False) else 500


def _coerce_schema(value: Any) -> dict[str, Any]:
    """Return a JSON-schema dict, defaulting to an open object schema."""
    return value if isinstance(value, dict) else {"type": "object"}


def _descriptor_dict(registry: Registry, module_id: str) -> dict[str, Any]:
    """Build a JSON-serializable descriptor (id, description, schemas, tags)."""
    definition = registry.get_definition(module_id)
    if definition is None:
        return {
            "module_id": module_id,
            "description": "",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "tags": [],
        }
    return {
        "module_id": module_id,
        "description": getattr(definition, "description", "") or "",
        "input_schema": _coerce_schema(getattr(definition, "input_schema", None)),
        "output_schema": _coerce_schema(getattr(definition, "output_schema", None)),
        "tags": list(getattr(definition, "tags", []) or []),
    }


def build_openapi(
    registry: Registry,
    *,
    title: str,
    version: str,
    description: str,
) -> dict[str, Any]:
    """Generate an OpenAPI 3.1 document from the registry's module schemas."""
    paths: dict[str, Any] = {}
    for module_id in registry.list():
        desc = _descriptor_dict(registry, module_id)
        summary = (desc["description"] or module_id).splitlines()[0][:120]
        paths[f"/modules/{module_id}"] = {
            "post": {
                "operationId": module_id.replace(".", "_"),
                "summary": summary,
                "description": desc["description"],
                "tags": desc["tags"] or ["modules"],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": desc["input_schema"]}},
                },
                "responses": {
                    "200": {
                        "description": "Module output",
                        "content": {"application/json": {"schema": desc["output_schema"]}},
                    },
                    "default": {
                        "description": "Error",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
                        },
                    },
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version, "description": description},
        "paths": paths,
        "components": {
            "schemas": {
                "Error": {
                    "type": "object",
                    "properties": {
                        "error": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "message": {"type": "string"},
                            },
                        }
                    },
                }
            }
        },
    }


def _read_inputs(raw_body: bytes) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Parse a JSON-object body. Returns (inputs, None) or (None, error_payload)."""
    if not raw_body:
        return {}, None
    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        return None, {"error": {"code": "INVALID_JSON", "message": f"Invalid JSON body: {exc}"}}
    if not isinstance(data, dict):
        return None, {
            "error": {"code": "INVALID_INPUT", "message": "Request body must be a JSON object"}
        }
    return data, None


async def _execute(
    executor: Executor, module_id: str, inputs: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Run the module and map the result/error to an HTTP status + JSON payload."""
    try:
        output = await executor.call_async(module_id, inputs)
    except ModuleError as exc:
        logger.info("REST execute %s failed [%s]: %s", module_id, exc.code, exc.message)
        return _status_for_error(exc), {"error": exc.to_dict()}
    except Exception as exc:  # noqa: BLE001 — surface unexpected errors as 500
        logger.error("REST execute %s crashed: %s", module_id, exc, exc_info=True)
        return 500, {"error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}}
    return 200, output


def _sse_event(data: dict[str, Any], *, event: Optional[str] = None) -> str:
    """Format a single Server-Sent Events frame."""
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(data)}\n\n"


async def _stream_events(
    executor: Executor, module_id: str, inputs: dict[str, Any]
) -> AsyncIterator[str]:
    """Stream a module's output as SSE frames.

    Streaming-capable modules emit one ``data`` frame per chunk; non-streaming
    modules emit a single ``data`` frame (the executor falls back to call_async).
    A terminal ``done`` event always closes the stream; errors arrive as an
    ``error`` event rather than corrupting the SSE framing.
    """
    try:
        async for chunk in executor.stream(module_id, inputs):
            yield _sse_event(chunk)
    except ModuleError as exc:
        logger.info("REST stream %s failed [%s]: %s", module_id, exc.code, exc.message)
        yield _sse_event({"error": exc.to_dict()}, event="error")
    except Exception as exc:  # noqa: BLE001 — surface as an SSE error event
        logger.error("REST stream %s crashed: %s", module_id, exc, exc_info=True)
        yield _sse_event(
            {"error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}}, event="error"
        )
    yield _sse_event({}, event="done")


_SWAGGER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>apflow API</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"/>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.ui = SwaggerUIBundle({ url: "/openapi.json", dom_id: "#swagger-ui" });
  </script>
</body>
</html>
"""


def build_rest_app(
    registry: Registry,
    *,
    title: str = "apflow",
    version: str = "",
    description: str = "apflow REST API (registry-driven)",
    cors_origins: Optional[list[str]] = None,
    extra_routes: Optional[list[Route]] = None,
    lifespan: Any = None,
) -> Starlette:
    """Build a Starlette app exposing every registry module over REST.

    Endpoints:
        GET  /              -- service info
        GET  /healthz       -- liveness probe
        GET  /openapi.json  -- generated OpenAPI 3.1 document
        GET  /docs          -- Swagger UI
        GET  /modules       -- list module descriptors
        GET  /modules/{id}  -- single module descriptor
        POST /modules/{id}  -- execute a module with a JSON-object body; returns
                               JSON, or an SSE event stream when the request's
                               Accept header is text/event-stream

    ``extra_routes`` are appended verbatim, letting callers mount non-registry
    operational endpoints (e.g. the inbound webhook) without this adapter
    depending on them. They are matched after the registry routes above.
    """
    executor = Executor.from_registry(registry)
    openapi_spec = build_openapi(registry, title=title, version=version, description=description)

    async def root(_request: Request) -> Response:
        return JSONResponse(
            {
                "name": title,
                "version": version,
                "modules": registry.count,
                "docs": "/docs",
                "openapi": "/openapi.json",
            }
        )

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def openapi_json(_request: Request) -> Response:
        return JSONResponse(openapi_spec)

    async def docs(_request: Request) -> Response:
        return HTMLResponse(_SWAGGER_HTML)

    async def list_modules(_request: Request) -> Response:
        modules = [_descriptor_dict(registry, mid) for mid in registry.list()]
        return JSONResponse({"modules": modules, "count": len(modules)})

    async def module_endpoint(request: Request) -> Response:
        module_id = request.path_params["module_id"]
        if not registry.has(module_id):
            return JSONResponse(
                {"error": {"code": "MODULE_NOT_FOUND", "message": f"No module {module_id!r}"}},
                status_code=404,
            )
        if request.method == "GET":
            return JSONResponse(_descriptor_dict(registry, module_id))

        inputs, error = _read_inputs(await request.body())
        if error is not None:
            return JSONResponse(error, status_code=400)
        assert inputs is not None  # _read_inputs returns one of (inputs, error)

        # Content negotiation: stream execution as SSE when the client asks for it.
        if "text/event-stream" in request.headers.get("accept", ""):
            return StreamingResponse(
                _stream_events(executor, module_id, inputs),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        status, payload = await _execute(executor, module_id, inputs)
        return JSONResponse(_json_safe(payload), status_code=status)

    routes = [
        Route("/", root, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/openapi.json", openapi_json, methods=["GET"]),
        Route("/docs", docs, methods=["GET"]),
        Route("/modules", list_modules, methods=["GET"]),
        Route("/modules/{module_id:path}", module_endpoint, methods=["GET", "POST"]),
    ]
    if extra_routes:
        routes.extend(extra_routes)

    middleware: list[Middleware] = []
    if cors_origins:
        from starlette.middleware.cors import CORSMiddleware

        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=cors_origins,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        )

    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


def serve_rest(
    registry: Registry,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    title: str = "apflow",
    version: str = "",
    cors_origins: Optional[list[str]] = None,
    log_level: Optional[str] = None,
    webhook: bool = False,
    webhook_secret: Optional[str] = None,
    auth: bool = False,
    scheduler: bool = False,
) -> None:
    """Build the REST app and run it under uvicorn (blocking).

    When ``webhook`` is set, the inbound ``POST /webhook/trigger/{task_id}``
    endpoint is mounted (optionally HMAC-guarded by ``webhook_secret``).

    When ``auth`` is set, module calls require a valid JWT Bearer token (built
    from ``api.jwt_*`` config); public metadata paths and the webhook are exempt.
    Raises RuntimeError if auth is requested without ``api.jwt_secret`` set.

    When ``scheduler`` is set, the built-in scheduler poll loop runs inside this
    server process (single-node consistency — one SQLite writer, no second
    process). For multi-node clusters use ``apflow worker`` instead.
    """
    import uvicorn

    extra_routes = None
    if webhook:
        from apflow.api.webhook import build_webhook_routes

        extra_routes = build_webhook_routes(secret_key=webhook_secret)

    lifespan = None
    if scheduler:
        from apflow.scheduler.internal import make_scheduler_lifespan

        lifespan = make_scheduler_lifespan()

    app: Any = build_rest_app(
        registry,
        title=title,
        version=version,
        cors_origins=cors_origins,
        extra_routes=extra_routes,
        lifespan=lifespan,
    )
    if auth:
        from apflow.api.auth import apply_rest_auth, build_mcp_authenticator

        authenticator = build_mcp_authenticator()
        if authenticator is None:
            raise RuntimeError("REST auth requested but api.jwt_secret is not configured")
        app = apply_rest_auth(app, authenticator)
    uvicorn.run(app, host=host, port=port, log_level=(log_level or "info").lower())
