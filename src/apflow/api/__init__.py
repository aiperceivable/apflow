"""apflow REST/HTTP API adapter (registry-driven, Starlette-based)."""

from apflow.api.combined import assemble_combined, serve_all
from apflow.api.rest import build_openapi, build_rest_app, serve_rest

__all__ = [
    "assemble_combined",
    "build_openapi",
    "build_rest_app",
    "serve_all",
    "serve_rest",
]
