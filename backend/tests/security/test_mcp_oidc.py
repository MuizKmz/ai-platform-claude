"""MCP verification against a real identity provider (ADR 0009 addendum).

Sibling of test_oidc.py, same infrastructure — a genuine RSA keypair, tokens
handed to the real verifier rather than a mock that would only agree with
itself. What differs here is the one thing that actually matters for MCP:
the AUDIENCE.

This file exists because `principal_from_mcp_token` was left verifying the
local HS256 issuer only when Keycloak first landed — found by calling the
running `/mcp` endpoint with a real Keycloak service-account token and
watching it get refused. The tests below are what should have caught that
before it shipped.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from app.core import oidc
from app.core.config import settings
from app.core.security import AuthError
from app.mcp.auth import mcp_audience, principal_from_mcp_token

pytestmark = pytest.mark.security

TENANT = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
USER = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
ISSUER = "https://idp.eaip.test/realms/eaip"
KID = "mcp-test-key-1"


@pytest.fixture(scope="module")
def keypair() -> tuple[Any, str]:
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private.public_key().public_numbers()

    def b64(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        import base64

        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    jwks = json.dumps(
        {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": KID,
                    "use": "sig",
                    "alg": "RS256",
                    "n": b64(numbers.n),
                    "e": b64(numbers.e),
                }
            ]
        }
    )
    return private, jwks


@pytest.fixture(autouse=True)
def provider(monkeypatch: pytest.MonkeyPatch, keypair: tuple[Any, str]) -> None:
    _, jwks = keypair
    monkeypatch.setattr(settings, "oidc_jwks_url", "https://idp.eaip.test/jwks", raising=False)
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER, raising=False)
    oidc.reset_key_cache()

    class _Response:
        def read(self) -> bytes:
            return jwks.encode()

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response())
    yield
    oidc.reset_key_cache()


def _sign(private: Any, *, aud: str, **overrides: Any) -> str:
    """A service-account-shaped token: no email claim of its own necessarily,
    but the same tenant_id/labels attributes a real user carries — set on the
    client's service-account user in Keycloak, exactly as ADR 0009's addendum
    describes."""
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": str(USER),
        "iss": ISSUER,
        "aud": aud,
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "email": "service-account-eaip-mcp@eaip.test",
        "https://eaip.dev/tenant_id": str(TENANT),
        "https://eaip.dev/labels": ["public", "iot"],
        "realm_access": {"roles": ["reader"]},
    }
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": KID})


def test_a_real_service_account_token_becomes_a_principal(keypair: tuple[Any, str]) -> None:
    """The fix, proven: a Keycloak service-account token for eaip-mcp, with
    the MCP audience, now verifies here — which it did not before this file's
    corresponding auth.py change existed."""
    private, _ = keypair
    principal = principal_from_mcp_token(_sign(private, aud=mcp_audience()))

    assert principal.tenant_id == TENANT
    assert principal.user_id == USER
    assert principal.allowed_labels == ("public", "iot")
    assert principal.roles == ("reader",)


def test_a_console_audienced_token_is_refused_here(keypair: tuple[Any, str]) -> None:
    """The confused-deputy case ADR 0007 exists to prevent, now reachable
    through a real IdP rather than only the local issuer: a token minted for
    eaip-console must not work as an MCP machine credential."""
    private, _ = keypair
    with pytest.raises(AuthError):
        principal_from_mcp_token(_sign(private, aud=settings.jwt_audience))


def test_an_mcp_audienced_token_is_refused_by_the_console_path(keypair: tuple[Any, str]) -> None:
    """The reverse direction of the same guarantee: an MCP token must not
    work as a console credential."""
    from app.core.oidc import principal_from_oidc_token

    private, _ = keypair
    mcp_token = _sign(private, aud=mcp_audience())
    with pytest.raises(AuthError):
        principal_from_oidc_token(mcp_token)  # defaults to the console audience


def test_no_provider_configured_falls_back_to_local_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without OIDC_JWKS_URL, the MCP path must still work exactly as it did
    before this file existed — the CLI's --mcp tokens keep working in
    development, and the test suite's own local-issuer tests are unaffected."""
    monkeypatch.setattr(settings, "oidc_jwks_url", "", raising=False)

    from app.core.security import issue_token

    token = issue_token(
        tenant_id=TENANT,
        user_id=USER,
        email="dev@eaip.test",
        roles=("reader",),
        allowed_labels=("public",),
        audience=mcp_audience(),
    )
    principal = principal_from_mcp_token(token)
    assert principal.tenant_id == TENANT
