"""apflow REST/HTTP API adapter (registry-driven, Starlette-based)."""

from apflow.api.combined import assemble_combined, serve_all
from apflow.api.rest import build_openapi, build_rest_app, serve_rest
from apflow.api.webhook import build_webhook_routes

__all__ = [
    "assemble_combined",
    "build_openapi",
    "build_rest_app",
    "build_webhook_routes",
    "serve_all",
    "serve_rest",
]
