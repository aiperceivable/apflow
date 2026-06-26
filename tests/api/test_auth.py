"""Tests for shared JWT auth on the REST face (apflow.api.auth).

Verifies the apcore-mcp AuthMiddleware wrapper gates module calls, accepts a
valid Bearer token, and exempts public metadata paths and the HMAC webhook.
The same JWTAuthenticator is reused by the MCP server and (as an a2a sibling)
by A2A, so one token authenticates everywhere.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import jwt
from apcore.registry.registry import Registry
from starlette.testclient import TestClient

from apflow.api.auth import apply_rest_auth, build_mcp_authenticator, resolve_verification_key
from apflow.api.rest import build_rest_app
from apflow.api.webhook import build_webhook_routes
from apflow.core.config_manager import ConfigManager

SECRET = "rest-auth-secret"


def _client() -> TestClient:
    cm = ConfigManager()
    cm.set("api.jwt_secret", SECRET)
    authenticator = build_mcp_authenticator(cm)
    app = build_rest_app(Registry(), title="apflow", extra_routes=build_webhook_routes())
    return TestClient(apply_rest_auth(app, authenticator))


def _token(**claims: object) -> str:
    return jwt.encode({"sub": "user1", **claims}, SECRET, algorithm="HS256")


def test_healthz_is_exempt() -> None:
    assert _client().get("/healthz").status_code == 200


def test_openapi_is_exempt() -> None:
    assert _client().get("/openapi.json").status_code == 200


def test_module_call_requires_token() -> None:
    assert _client().post("/modules/x", json={}).status_code == 401


def test_invalid_token_rejected() -> None:
    resp = _client().post("/modules/x", json={}, headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


def test_valid_token_passes_auth() -> None:
    # Empty registry → MODULE_NOT_FOUND (404). Reaching that 404 (not 401) proves
    # the request cleared the auth layer.
    resp = _client().post("/modules/x", json={}, headers={"Authorization": f"Bearer {_token()}"})
    assert resp.status_code == 404


def test_webhook_is_exempt_from_jwt() -> None:
    # The webhook carries its own HMAC auth, so the JWT layer must let it through
    # untouched (no 401). Gateway execution is mocked to avoid DB access.
    with patch(
        "apflow.scheduler.gateway.webhook.WebhookGateway.trigger_task",
        new=AsyncMock(return_value={"success": True, "status": "triggered"}),
    ):
        resp = _client().post("/webhook/trigger/t1")
    assert resp.status_code == 200


# --- Algorithm key resolution (HS256 symmetric vs RS256 asymmetric) ---


def test_resolve_key_hs256_uses_secret() -> None:
    cm = ConfigManager()
    cm.set("api.jwt_algorithm", "HS256")
    cm.set("api.jwt_secret", "shh")
    assert resolve_verification_key(cm) == "shh"


def test_resolve_key_rs256_uses_public_key_not_secret() -> None:
    cm = ConfigManager()
    cm.set("api.jwt_algorithm", "RS256")
    cm.set("api.jwt_secret", "ignored-for-asymmetric")
    cm.set("api.jwt_public_key", "-----BEGIN PUBLIC KEY-----\nABC\n-----END PUBLIC KEY-----")
    assert "BEGIN PUBLIC KEY" in (resolve_verification_key(cm) or "")


def test_resolve_key_rs256_from_file(tmp_path) -> None:
    pem = tmp_path / "pub.pem"
    pem.write_text("-----BEGIN PUBLIC KEY-----\nFROMFILE\n-----END PUBLIC KEY-----")
    cm = ConfigManager()
    cm.set("api.jwt_algorithm", "RS256")
    cm.set("api.jwt_public_key_path", str(pem))
    assert "FROMFILE" in (resolve_verification_key(cm) or "")


def test_resolve_key_rs256_missing_public_key_is_none() -> None:
    cm = ConfigManager()
    cm.set("api.jwt_algorithm", "RS256")
    cm.set("api.jwt_secret", "not-a-public-key")
    assert resolve_verification_key(cm) is None  # secret must not be used for RS256


def _rsa_keypair() -> tuple[str, str]:
    """Generate a throwaway RSA keypair as (private_pem, public_pem)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def test_rs256_round_trip() -> None:
    """A token signed with the RSA private key verifies via the configured public key."""
    import pytest

    pytest.importorskip("cryptography")
    private_pem, public_pem = _rsa_keypair()

    cm = ConfigManager()
    cm.set("api.jwt_algorithm", "RS256")
    cm.set("api.jwt_public_key", public_pem)
    authenticator = build_mcp_authenticator(cm)
    app = build_rest_app(Registry(), title="apflow")
    client = TestClient(apply_rest_auth(app, authenticator))

    token = jwt.encode({"sub": "svc"}, private_pem, algorithm="RS256")
    # Valid RS256 token clears auth → 404 (module not in empty registry).
    ok = client.post("/modules/x", json={}, headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 404
    # No token → 401.
    assert client.post("/modules/x", json={}).status_code == 401
    # A token signed with the WRONG key is rejected.
    wrong_priv, _ = _rsa_keypair()
    bad = jwt.encode({"sub": "svc"}, wrong_priv, algorithm="RS256")
    resp = client.post("/modules/x", json={}, headers={"Authorization": f"Bearer {bad}"})
    assert resp.status_code == 401


def test_mixed_algorithm_families_rejected() -> None:
    """A mixed HS+RS algorithm list is rejected (JWT algorithm-confusion guard).

    With api.jwt_algorithm="HS256,RS256" the verification key is the RSA public
    key, yet HS256 would let an attacker HMAC-sign a token with that public key.
    The config must be refused rather than silently enabling the attack.
    """
    import pytest

    cm = ConfigManager()
    cm.set("api.jwt_algorithm", "HS256,RS256")
    cm.set("api.jwt_public_key", "-----BEGIN PUBLIC KEY-----\nABC\n-----END PUBLIC KEY-----")
    with pytest.raises(ValueError, match="mix"):
        resolve_verification_key(cm)
    with pytest.raises(ValueError, match="mix"):
        build_mcp_authenticator(cm)


def test_same_family_algorithm_list_allowed() -> None:
    """A same-family list (e.g. RS256,RS512) is fine — no confusion possible."""
    cm = ConfigManager()
    cm.set("api.jwt_algorithm", "RS256,RS512")
    cm.set("api.jwt_public_key", "-----BEGIN PUBLIC KEY-----\nABC\n-----END PUBLIC KEY-----")
    assert "BEGIN PUBLIC KEY" in (resolve_verification_key(cm) or "")
