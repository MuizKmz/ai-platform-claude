"""Authentication and server-derived tenancy.

Security invariant #1: tenant_id comes from the verified token and nowhere else.
These tests attack that claim from every direction a caller controls.
"""

import uuid
from datetime import timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core import security
from app.core.config import settings
from app.core.security import AuthError, Principal, issue_token, principal_from_token
from app.main import app

pytestmark = pytest.mark.security

TENANT_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TENANT_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A = uuid.UUID("11111111-2222-3333-4444-555555555555")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _token_a(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "tenant_id": TENANT_A,
        "user_id": USER_A,
        "email": "alice@acme.test",
        "roles": ("reader",),
        "allowed_labels": ("public",),
    }
    kwargs.update(overrides)
    return issue_token(**kwargs)  # type: ignore[arg-type]


# --- the happy path, so the failures below mean something ---------------------


def test_valid_token_yields_the_expected_principal(client: TestClient) -> None:
    response = client.get("/v1/me", headers={"Authorization": f"Bearer {_token_a()}"})

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == str(TENANT_A)
    assert body["email"] == "alice@acme.test"
    assert body["allowed_labels"] == ["public"]


# --- absence of credentials ---------------------------------------------------


def test_no_token_is_401(client: TestClient) -> None:
    assert client.get("/v1/me").status_code == 401


def test_malformed_authorization_header_is_401(client: TestClient) -> None:
    for header in ("", "Bearer", "Bearer ", "Basic abc", "Bearer not.a.jwt"):
        assert client.get("/v1/me", headers={"Authorization": header}).status_code == 401, header


# --- forging tenancy through request data ------------------------------------


def test_tenant_id_in_body_or_query_is_ignored(client: TestClient) -> None:
    """The core of invariant #1.

    A caller holding a valid token for tenant A supplies tenant B everywhere a
    caller can supply anything. The response must still say tenant A.
    """
    response = client.get(
        f"/v1/me?tenant_id={TENANT_B}",
        headers={
            "Authorization": f"Bearer {_token_a()}",
            "X-Tenant-Id": str(TENANT_B),
            "X-Tenant-ID": str(TENANT_B),
        },
    )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == str(TENANT_A), "request data overrode token tenancy"


def test_tampered_payload_is_rejected(client: TestClient) -> None:
    """Editing the claims invalidates the signature."""
    token = _token_a()
    header, payload, sig = token.split(".")
    forged_payload = (
        jwt.utils.base64url_encode(
            jwt.utils.base64url_decode(payload).replace(
                str(TENANT_A).encode(), str(TENANT_B).encode()
            )
        )
        .decode()
        .rstrip("=")
    )

    response = client.get(
        "/v1/me", headers={"Authorization": f"Bearer {header}.{forged_payload}.{sig}"}
    )

    assert response.status_code == 401


# --- attacks on the verification itself ---------------------------------------


def test_alg_none_token_is_rejected() -> None:
    """The classic JWT attack: an unsigned token claiming it needs no signature.

    Verification pins the algorithm list, so the token's own header cannot choose.
    """
    unsigned = jwt.encode(
        {
            "sub": str(USER_A),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "exp": 9999999999,
            "iat": 0,
            "https://eaip.dev/tenant_id": str(TENANT_B),
            "https://eaip.dev/email": "attacker@evil.test",
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(AuthError):
        principal_from_token(unsigned)


def test_token_signed_with_the_wrong_secret_is_rejected() -> None:
    forged = jwt.encode(
        {
            "sub": str(USER_A),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "exp": 9999999999,
            "iat": 0,
            "https://eaip.dev/tenant_id": str(TENANT_B),
            "https://eaip.dev/email": "attacker@evil.test",
        },
        key="not-the-real-secret",
        algorithm="HS256",
    )

    with pytest.raises(AuthError):
        principal_from_token(forged)


def test_expired_token_is_rejected() -> None:
    expired = _token_a(expires_in=timedelta(seconds=-1))

    with pytest.raises(AuthError):
        principal_from_token(expired)


def test_token_from_another_issuer_or_audience_is_rejected() -> None:
    """Prevents a token minted for a different service being replayed here."""
    for claim, value in (("iss", "https://evil.test"), ("aud", "some-other-api")):
        payload = {
            "sub": str(USER_A),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "exp": 9999999999,
            "iat": 0,
            "https://eaip.dev/tenant_id": str(TENANT_A),
            "https://eaip.dev/email": "alice@acme.test",
        }
        payload[claim] = value
        token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")

        with pytest.raises(AuthError):
            principal_from_token(token)


def test_signed_token_without_tenant_claim_is_rejected() -> None:
    """A validly signed token that carries no tenancy must not become a Principal.

    Otherwise "no tenant" could quietly become "any tenant" downstream.
    """
    token = jwt.encode(
        {
            "sub": str(USER_A),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "exp": 9999999999,
            "iat": 0,
            "https://eaip.dev/email": "alice@acme.test",
        },
        settings.jwt_secret,
        algorithm="HS256",
    )

    with pytest.raises(AuthError):
        principal_from_token(token)


# --- the Principal itself -----------------------------------------------------


def test_principal_is_immutable() -> None:
    """A mutable Principal is a Principal whose tenant_id can be reassigned."""
    p = Principal(tenant_id=TENANT_A, user_id=USER_A, email="alice@acme.test")

    with pytest.raises((AttributeError, TypeError)):
        p.tenant_id = TENANT_B  # type: ignore[misc]


def test_error_response_does_not_leak_why(client: TestClient) -> None:
    """Every failure returns the same opaque 401.

    Distinguishing "expired" from "bad signature" from "wrong audience" hands an
    attacker a debugging oracle.
    """
    bodies = set()
    for token in (
        "garbage",
        _token_a(expires_in=timedelta(seconds=-1)),
        jwt.encode({"sub": "x"}, "wrong-secret", algorithm="HS256"),
    ):
        r = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
        bodies.add(r.text)

    assert len(bodies) == 1, f"401 responses differ and reveal the cause: {bodies}"


def test_secret_never_appears_in_a_response(client: TestClient) -> None:
    """Invariant #5, checked at the boundary that actually returns bytes."""
    r = client.get("/v1/me", headers={"Authorization": "Bearer garbage"})

    assert settings.jwt_secret not in r.text
    assert security.ALGORITHM == "HS256"
