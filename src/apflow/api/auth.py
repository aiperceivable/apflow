"""Shared JWT authentication wiring for apflow's REST, A2A, and MCP faces.

All three protocols validate the SAME Bearer token, built from ConfigManager's
``api.jwt_*`` settings, so one token minted with ``api.jwt_secret`` authenticates
everywhere:

  - REST : ``apcore_mcp.auth.AuthMiddleware`` wraps the Starlette app
  - A2A  : ``apcore_a2a.auth.JWTAuthenticator`` passed to ``a2a serve(auth=...)``
  - MCP  : ``apcore_mcp.auth.JWTAuthenticator`` passed to ``mcp serve(authenticator=...)``

The two SDK ``JWTAuthenticator`` classes share an identical constructor
(``key``, ``algorithms``, ``audience``, ``issuer``) and verify the same token;
REST reuses the apcore-mcp authenticator via its ASGI ``AuthMiddleware``.

When no secret is configured the builders return ``None`` (auth disabled — the
caller decides whether that is a hard error or a dev-mode warning).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from apflow.core.config_manager import ConfigManager, get_config_manager

# Public REST paths that must stay reachable without a token: service metadata,
# liveness probe, and the self-documenting OpenAPI / Swagger surface.
REST_EXEMPT_PATHS: set[str] = {"/", "/healthz", "/docs", "/openapi.json"}
# The inbound webhook authenticates standalone via HMAC, so it bypasses JWT to
# avoid forcing external schedulers to carry both credentials.
REST_EXEMPT_PREFIXES: set[str] = {"/webhook"}


def _algorithms(cm: ConfigManager) -> list[str]:
    """Parse the configured algorithm(s), supporting a comma-separated list."""
    return [a.strip() for a in cm.jwt_algorithm.split(",") if a.strip()]


def _reject_mixed_algorithm_families(algorithms: list[str]) -> None:
    """Refuse a list mixing symmetric (HS*) and asymmetric (RS*/ES*/PS*) algorithms.

    A mixed list is a JWT algorithm-confusion hazard: the verification key would
    be the asymmetric *public* key, but an HS* entry lets an attacker HMAC-sign a
    token with that public key (which is public by design) and be accepted. The
    two families use fundamentally different keys, so a single config can only
    safely use one of them.
    """
    has_symmetric = any(a.upper().startswith("HS") for a in algorithms)
    has_asymmetric = any(not a.upper().startswith("HS") for a in algorithms)
    if has_symmetric and has_asymmetric:
        raise ValueError(
            "api.jwt_algorithm must not mix symmetric (HS*) and asymmetric "
            f"(RS*/ES*/PS*) algorithms: {algorithms}. They use different keys; "
            "mixing them enables JWT algorithm-confusion attacks. Configure one "
            "family only."
        )


def resolve_verification_key(cm: ConfigManager) -> Optional[str]:
    """Return the key used to VERIFY tokens, picked by algorithm family.

    Symmetric (HS*) verifies with the shared ``api.jwt_secret``. Asymmetric
    (RS*/ES*/PS*) verifies with a public key — provided inline via
    ``api.jwt_public_key`` or read from ``api.jwt_public_key_path``. Returns
    None when the relevant key is not configured (auth cannot be enabled).
    Raises ValueError on a mixed-family algorithm list (algorithm-confusion guard).
    """
    algorithms = _algorithms(cm)
    _reject_mixed_algorithm_families(algorithms)
    is_asymmetric = any(not a.upper().startswith("HS") for a in algorithms)
    if is_asymmetric:
        if cm.jwt_public_key:
            return cm.jwt_public_key
        if cm.jwt_public_key_path:
            return Path(cm.jwt_public_key_path).read_text(encoding="utf-8")
        return None
    return cm.jwt_secret


def _jwt_kwargs(cm: ConfigManager) -> Optional[dict[str, Any]]:
    """Read JWT settings into JWTAuthenticator kwargs, or None when no key."""
    key = resolve_verification_key(cm)
    if not key:
        return None
    return {
        "key": key,
        "algorithms": _algorithms(cm),
        "audience": cm.jwt_audience,
        "issuer": cm.jwt_issuer,
    }


def build_mcp_authenticator(cm: Optional[ConfigManager] = None) -> Optional[Any]:
    """Build an apcore-mcp JWTAuthenticator from config (None if no secret).

    Used both for the MCP server and, via ``apply_rest_auth``, for REST.
    """
    kwargs = _jwt_kwargs(cm or get_config_manager())
    if kwargs is None:
        return None
    from apcore_mcp.auth import JWTAuthenticator

    return JWTAuthenticator(**kwargs)


def build_a2a_authenticator(cm: Optional[ConfigManager] = None) -> Optional[Any]:
    """Build an apcore-a2a JWTAuthenticator from config (None if no secret)."""
    kwargs = _jwt_kwargs(cm or get_config_manager())
    if kwargs is None:
        return None
    from apcore_a2a.auth import JWTAuthenticator

    return JWTAuthenticator(**kwargs)


def apply_rest_auth(
    app: Any,
    authenticator: Any,
    *,
    extra_exempt_prefixes: Optional[set[str]] = None,
) -> Any:
    """Wrap a Starlette/ASGI app so REST module calls require a valid Bearer token.

    Public metadata paths (:data:`REST_EXEMPT_PATHS`) and the HMAC-guarded
    webhook (:data:`REST_EXEMPT_PREFIXES`) bypass JWT. ``extra_exempt_prefixes``
    lets the combined server also exempt the A2A / MCP sub-apps, which carry
    their own authentication.
    """
    from apcore_mcp.auth import AuthMiddleware

    prefixes = set(REST_EXEMPT_PREFIXES)
    if extra_exempt_prefixes:
        prefixes |= extra_exempt_prefixes
    return AuthMiddleware(
        app,
        authenticator,
        exempt_paths=set(REST_EXEMPT_PATHS),
        exempt_prefixes=prefixes,
        require_auth=True,
    )
