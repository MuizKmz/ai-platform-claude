"""Verification against a real identity provider (ADR 0009).

These tests mint tokens with a genuine RSA keypair and hand the public half to
the verifier, so the signature check is really exercised rather than mocked. A
mock here would assert that our own stub agrees with itself.

The attacks below are the ones that have actually broken JWT deployments:
algorithm confusion, `alg: none`, audience reuse across surfaces, and a signed
token that is missing the tenancy claim everything downstream depends on.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core import oidc
from app.core.config import settings
from app.core.security import AuthError

pytestmark = pytest.mark.security

TENANT = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
USER = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
ISSUER = "https://idp.eaip.test/realms/eaip"
KID = "test-key-1"


@pytest.fixture(scope="module")
def keypair() -> tuple[Any, str]:
    """A real RSA keypair, and its public half as a JWKS document."""
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
    """Point the verifier at our in-memory key set."""
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

    # PyJWKClient fetches with urllib.request.urlopen under the hood. Patch the
    # module attribute it resolves at call time, not a string path.
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response())
    yield
    oidc.reset_key_cache()


def _sign(private: Any, *, algorithm: str = "RS256", **overrides: Any) -> str:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": str(USER),
        "iss": ISSUER,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "email": "alice@acme.test",
        "https://eaip.dev/tenant_id": str(TENANT),
        "https://eaip.dev/labels": ["public", "iot"],
        "realm_access": {"roles": ["analyst"]},
    }
    claims.update(overrides)
    key = private if algorithm == "RS256" else "a-shared-secret"
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": KID})


# --- the happy path -----------------------------------------------------------


def test_a_provider_token_becomes_a_principal(keypair: tuple[Any, str]) -> None:
    private, _ = keypair
    principal = oidc.principal_from_oidc_token(_sign(private))

    assert principal.tenant_id == TENANT
    assert principal.user_id == USER
    assert principal.email == "alice@acme.test"
    assert principal.roles == ("analyst",)
    assert principal.allowed_labels == ("public", "iot")


# --- the attacks --------------------------------------------------------------


def test_alg_none_is_refused(keypair: tuple[Any, str]) -> None:
    """The classic. An unsigned token must never verify."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    unsigned = jwt.encode(
        {
            "sub": str(USER),
            "iss": ISSUER,
            "aud": settings.jwt_audience,
            "iat": now,
            "exp": now + timedelta(minutes=10),
            "https://eaip.dev/tenant_id": str(TENANT),
            "email": "attacker@evil.test",
        },
        key="",
        algorithm="none",
        headers={"kid": KID},
    )
    with pytest.raises(AuthError):
        oidc.principal_from_oidc_token(unsigned)


def test_symmetric_signature_against_the_public_key_is_refused(
    keypair: tuple[Any, str],
) -> None:
    """Algorithm confusion.

    An attacker who reads the *public* key — it is published, by design — signs
    HS256 with it and hopes the verifier treats it as the shared secret. Pinning
    to RS256 is what stops this.
    """
    private, _ = keypair
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)

    # Built by hand: PyJWT refuses to *encode* this, having spotted an RSA key
    # being passed as an HMAC secret. That is a good library, but it means
    # jwt.encode cannot produce the attack — and the verifier is what is under
    # test here, not the encoder.
    import base64
    import hashlib
    import hmac

    def _segment(payload: dict[str, Any]) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")

    header = _segment({"alg": "HS256", "typ": "JWT", "kid": KID})
    body = _segment(
        {
            "sub": str(USER),
            "iss": ISSUER,
            "aud": settings.jwt_audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=10)).timestamp()),
            "https://eaip.dev/tenant_id": str(TENANT),
            "email": "attacker@evil.test",
            "realm_access": {"roles": ["admin"]},
        }
    )
    signing_input = header + b"." + body
    signature = base64.urlsafe_b64encode(
        hmac.new(public_pem.encode(), signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    forged = (signing_input + b"." + signature).decode()

    # Refused either at key resolution or at signature check — both are the
    # RS256 pin doing its job. What must never happen is a Principal, and
    # especially not the admin one these claims ask for.
    with pytest.raises(AuthError):
        oidc.principal_from_oidc_token(forged)


def test_a_token_signed_by_another_key_is_refused() -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthError):
        oidc.principal_from_oidc_token(_sign(other))


def test_wrong_issuer_is_refused(keypair: tuple[Any, str]) -> None:
    private, _ = keypair
    with pytest.raises(AuthError):
        oidc.principal_from_oidc_token(_sign(private, iss="https://evil.test/realms/eaip"))


def test_wrong_audience_is_refused(keypair: tuple[Any, str]) -> None:
    """A console token must not work as an MCP machine credential (ADR 0007)."""
    private, _ = keypair
    with pytest.raises(AuthError):
        oidc.principal_from_oidc_token(_sign(private, aud="eaip-mcp"))


def test_expired_is_refused(keypair: tuple[Any, str]) -> None:
    from datetime import UTC, datetime, timedelta

    private, _ = keypair
    past = datetime.now(UTC) - timedelta(hours=2)
    with pytest.raises(AuthError):
        oidc.principal_from_oidc_token(_sign(private, iat=past, exp=past + timedelta(minutes=10)))


def test_a_signed_token_without_tenancy_is_refused(keypair: tuple[Any, str]) -> None:
    """Invariant #1. A valid signature is not an identity.

    The realm's claim mappers can be misconfigured; that must produce a refused
    login, never a Principal with a defaulted tenant.
    """
    private, _ = keypair
    token = _sign(private)
    claims = jwt.decode(token, options={"verify_signature": False}, audience=settings.jwt_audience)
    del claims["https://eaip.dev/tenant_id"]
    stripped = jwt.encode(claims, private, algorithm="RS256", headers={"kid": KID})

    with pytest.raises(AuthError):
        oidc.principal_from_oidc_token(stripped)


def test_a_nonsense_tenant_claim_is_refused(keypair: tuple[Any, str]) -> None:
    private, _ = keypair
    with pytest.raises(AuthError):
        oidc.principal_from_oidc_token(
            _sign(private, **{"https://eaip.dev/tenant_id": "not-a-uuid"})
        )


# --- claim shapes -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"roles": ["admin", "analyst"]}, ("admin", "analyst")),
        (["admin", "analyst"], ("admin", "analyst")),
        ("admin analyst", ("admin", "analyst")),
        (None, ()),
        (12345, ()),
    ],
)
def test_roles_survive_whichever_shape_the_provider_sends(
    keypair: tuple[Any, str], value: Any, expected: tuple[str, ...]
) -> None:
    """Providers disagree about this, and none of the disagreements should 500."""
    private, _ = keypair
    principal = oidc.principal_from_oidc_token(_sign(private, realm_access=value))
    assert principal.roles == expected


def test_an_unreadable_provider_is_a_config_error_not_a_401(
    monkeypatch: pytest.MonkeyPatch, keypair: tuple[Any, str]
) -> None:
    """A 401 would send an operator hunting for a bad token that does not exist."""
    import urllib.request
    from urllib.error import URLError

    private, _ = keypair
    # Force a real fetch: an already-cached key set would answer without ever
    # reaching the provider, and the test would pass for the wrong reason.
    oidc.reset_key_cache()

    def _boom(*_a: object, **_k: object) -> None:
        raise URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    with pytest.raises(oidc.OIDCConfigurationError):
        oidc.principal_from_oidc_token(_sign(private))
