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
from typing import Any, Optional

from apcore.errors import ModuleError
from apcore.executor import Executor
from apcore.registry.registry import Registry
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
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


async def _execute(
    executor: Executor, module_id: str, raw_body: bytes
) -> tuple[int, dict[str, Any]]:
    """Parse the request body, run the module, and map the result/error to HTTP."""
    if raw_body:
        try:
            inputs = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            return 400, {"error": {"code": "INVALID_JSON", "message": f"Invalid JSON body: {exc}"}}
    else:
        inputs = {}
    if not isinstance(inputs, dict):
        return 400, {
            "error": {"code": "INVALID_INPUT", "message": "Request body must be a JSON object"}
        }

    try:
        output = await executor.call_async(module_id, inputs)
    except ModuleError as exc:
        logger.info("REST execute %s failed [%s]: %s", module_id, exc.code, exc.message)
        return _status_for_error(exc), {"error": exc.to_dict()}
    except Exception as exc:  # noqa: BLE001 — surface unexpected errors as 500
        logger.error("REST execute %s crashed: %s", module_id, exc, exc_info=True)
        return 500, {"error": {"code": "INTERNAL_ERROR", "message": str(exc)}}
    return 200, output


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
) -> Starlette:
    """Build a Starlette app exposing every registry module over REST.

    Endpoints:
        GET  /              -- service info
        GET  /healthz       -- liveness probe
        GET  /openapi.json  -- generated OpenAPI 3.1 document
        GET  /docs          -- Swagger UI
        GET  /modules       -- list module descriptors
        GET  /modules/{id}  -- single module descriptor
        POST /modules/{id}  -- execute a module with a JSON-object body
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
        status, payload = await _execute(executor, module_id, await request.body())
        return JSONResponse(payload, status_code=status)

    routes = [
        Route("/", root, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/openapi.json", openapi_json, methods=["GET"]),
        Route("/docs", docs, methods=["GET"]),
        Route("/modules", list_modules, methods=["GET"]),
        Route("/modules/{module_id:path}", module_endpoint, methods=["GET", "POST"]),
    ]

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

    return Starlette(routes=routes, middleware=middleware)


def serve_rest(
    registry: Registry,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    title: str = "apflow",
    version: str = "",
    cors_origins: Optional[list[str]] = None,
    log_level: Optional[str] = None,
) -> None:
    """Build the REST app and run it under uvicorn (blocking)."""
    import uvicorn

    app = build_rest_app(registry, title=title, version=version, cors_origins=cors_origins)
    uvicorn.run(app, host=host, port=port, log_level=(log_level or "info").lower())
