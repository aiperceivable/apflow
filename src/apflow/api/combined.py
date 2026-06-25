"""
Unified REST + A2A + MCP server for apflow.

Mounts the registry-driven REST app (apflow.api.rest), the apcore-a2a ASGI app,
and the apcore-mcp ASGI app under a single Starlette application served by one
uvicorn process:

    /        -> REST/JSON + OpenAPI docs   (apflow.api.rest)
    /a2a     -> A2A agent                  (apcore-a2a)
    /mcp     -> MCP streamable-http server (apcore-mcp)

Follows the embedding pattern documented by ``apcore_mcp.async_serve``: the MCP
app is an async context manager whose transport session manager must stay alive
for the lifetime of the server, so uvicorn runs *inside* the ``async with`` block.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from apcore.registry.registry import Registry
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount

from apflow.api.rest import build_rest_app
from apflow.logger import get_logger

logger = get_logger(__name__)


def assemble_combined(
    rest_app: Starlette,
    a2a_app: Starlette,
    mcp_app: Starlette,
    *,
    cors_origins: Optional[list[str]] = None,
) -> Starlette:
    """Mount A2A, REST, and MCP under one Starlette with clean top-level paths.

    apcore-mcp hardcodes its streamable-http endpoint at ``/mcp`` inside the MCP
    app (alongside ``/health`` and ``/metrics``), so mounting that app under a
    ``/mcp`` prefix would bury the endpoint at ``/mcp/mcp``. Instead the MCP app
    is mounted at the root as a catch-all and the lightweight REST routes are
    lifted ahead of it, so the surface is::

        /, /docs, /openapi.json, /modules*  -> REST   (lifted routes)
        /a2a/*                              -> A2A     (mounted, middleware kept)
        /mcp, /health, /metrics             -> MCP     (root mount, middleware kept)

    A2A and MCP keep their own middleware via mounting; REST carries none of note
    (optional CORS is applied here at the parent instead).
    """
    routes = [
        Mount("/a2a", app=a2a_app),
        *rest_app.routes,
        Mount("/", app=mcp_app),
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


async def _serve_all_async(
    registry: Registry,
    *,
    host: str,
    port: int,
    title: str,
    version: str,
    description: str,
    cors_origins: Optional[list[str]],
    log_level: Optional[str],
    explorer: bool,
    metrics: bool,
) -> None:
    """Assemble the combined app and run uvicorn inside the MCP async context."""
    import uvicorn
    from apcore_a2a import async_serve as a2a_async_serve
    from apcore_mcp import async_serve as mcp_async_serve

    base_url = f"http://{host}:{port}"
    # CORS is applied once at the combined parent (assemble_combined), so the REST
    # sub-app is built without its own CORS layer here.
    rest_app = build_rest_app(registry, title=title, version=version)
    a2a_app = await a2a_async_serve(
        registry,
        name=title,
        version=version,
        description=description,
        url=f"{base_url}/a2a",
        explorer=explorer,
        metrics=metrics,
        cors_origins=cors_origins,
    )

    # The MCP transport session manager lives for the duration of this block, so
    # uvicorn must run inside it (per apcore_mcp.async_serve's documented pattern).
    async with mcp_async_serve(
        registry,
        name=title,
        version=version,
        explorer=explorer,
        observability=metrics,
    ) as mcp_app:
        combined = assemble_combined(rest_app, a2a_app, mcp_app, cors_origins=cors_origins)
        config = uvicorn.Config(
            combined, host=host, port=port, log_level=(log_level or "info").lower()
        )
        await uvicorn.Server(config).serve()


def serve_all(
    registry: Registry,
    *,
    host: str = "0.0.0.0",
    port: int = 8000,
    title: str = "apflow",
    version: str = "",
    description: str = "apflow AI-Perceivable Distributed Orchestration",
    cors_origins: Optional[list[str]] = None,
    log_level: Optional[str] = None,
    explorer: bool = False,
    metrics: bool = False,
) -> None:
    """Build and run the unified REST + A2A + MCP server on one port (blocking)."""
    asyncio.run(
        _serve_all_async(
            registry,
            host=host,
            port=port,
            title=title,
            version=version,
            description=description,
            cors_origins=cors_origins,
            log_level=log_level,
            explorer=explorer,
            metrics=metrics,
        )
    )
