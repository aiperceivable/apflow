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
from typing import Any, Optional

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
    sys_modules: bool,
    push_notifications: bool,
    webhook: bool,
    webhook_secret: Optional[str],
    auth: bool,
    scheduler: bool,
) -> None:
    """Assemble the combined app and run uvicorn inside the MCP async context.

    When ``auth`` is set, every face validates the same JWT: A2A and MCP each
    carry their own authenticator (so their sub-apps self-authenticate, keeping
    A2A agent-card discovery exempt), while a parent AuthMiddleware guards the
    REST module routes. The parent exempts the /a2a and /mcp prefixes (handled
    by the sub-apps), the HMAC webhook, and the public REST metadata paths.
    Raises RuntimeError if auth is requested without ``api.jwt_secret`` set.
    """
    import uvicorn
    from apcore_a2a import async_serve as a2a_async_serve
    from apcore_mcp import async_serve as mcp_async_serve

    base_url = f"http://{host}:{port}"
    # The webhook route is lifted to the top level alongside the other REST routes
    # by assemble_combined (*rest_app.routes), so it serves at /webhook/trigger/...
    extra_routes = None
    if webhook:
        from apflow.api.webhook import build_webhook_routes

        extra_routes = build_webhook_routes(secret_key=webhook_secret)

    a2a_auth = None
    mcp_auth = None
    if auth:
        from apflow.api.auth import build_a2a_authenticator, build_mcp_authenticator

        a2a_auth = build_a2a_authenticator()
        mcp_auth = build_mcp_authenticator()
        if a2a_auth is None or mcp_auth is None:
            # Both builders return None when api.jwt_secret is not configured.
            # Pressing on here would silently leave A2A/MCP unauthenticated
            # (auth=None/require_auth with no authenticator) and wrap REST in
            # an AuthMiddleware backed by a None authenticator, crashing every
            # request with a 500 — fail loudly instead, matching serve_rest.
            raise RuntimeError("auth requested but api.jwt_secret is not configured")

    # CORS is applied once at the combined parent (assemble_combined), so the REST
    # sub-app is built without its own CORS layer here.
    rest_app = build_rest_app(registry, title=title, version=version, extra_routes=extra_routes)
    a2a_app = await a2a_async_serve(
        registry,
        name=title,
        version=version,
        description=description,
        url=f"{base_url}/a2a",
        explorer=explorer,
        metrics=metrics,
        sys_modules=sys_modules,
        cors_origins=cors_origins,
        push_notifications=push_notifications,
        auth=a2a_auth,
    )

    # The MCP transport session manager lives for the duration of this block, so
    # uvicorn must run inside it (per apcore_mcp.async_serve's documented pattern).
    async with mcp_async_serve(
        registry,
        name=title,
        version=version,
        explorer=explorer,
        allow_execute=explorer,
        observability=metrics,
        authenticator=mcp_auth,
        require_auth=auth,
    ) as mcp_app:
        combined = assemble_combined(rest_app, a2a_app, mcp_app, cors_origins=cors_origins)
        app: Any = combined
        if auth:
            from apflow.api.auth import apply_rest_auth

            # A2A and MCP self-authenticate inside their mounted sub-apps, so the
            # parent only needs to guard the lifted REST module routes; exempt the
            # sub-app prefixes (and MCP's health/metrics) to avoid double-gating.
            app = apply_rest_auth(
                combined,
                mcp_auth,
                extra_exempt_prefixes={"/a2a", "/mcp", "/health", "/metrics", "/.well-known"},
            )
        config = uvicorn.Config(app, host=host, port=port, log_level=(log_level or "info").lower())

        # Run the scheduler poll loop in this same process/event loop (single
        # SQLite writer) for the lifetime of the server; clusters use worker.
        scheduler_obj = None
        if scheduler:
            from apflow.scheduler.internal import InternalScheduler

            scheduler_obj = InternalScheduler()
            await scheduler_obj.start()
        try:
            await uvicorn.Server(config).serve()
        finally:
            if scheduler_obj is not None:
                await scheduler_obj.stop()


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
    sys_modules: bool = False,
    push_notifications: bool = False,
    webhook: bool = False,
    webhook_secret: Optional[str] = None,
    auth: bool = False,
    scheduler: bool = False,
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
            sys_modules=sys_modules,
            push_notifications=push_notifications,
            webhook=webhook,
            webhook_secret=webhook_secret,
            auth=auth,
            scheduler=scheduler,
        )
    )
