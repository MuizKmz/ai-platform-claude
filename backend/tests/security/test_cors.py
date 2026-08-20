"""CORS: an allowlist, never a wildcard.

This API is authenticated by a bearer token the browser holds. With
`allow_origins=["*"]`, any site a user visits could read their tenant's data
using that token — the browser would happily hand the response to attacker.com
because the API said it was fine.

So the wildcard test matters more than the happy-path one. A permissive CORS
config is a single character away and looks harmless in a diff.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

pytestmark = pytest.mark.security

ALLOWED = "http://localhost:3001"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_configured_origin_is_allowed(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": ALLOWED})

    assert response.headers.get("access-control-allow-origin") == ALLOWED


def test_unknown_origin_gets_no_cors_headers(client: TestClient) -> None:
    """No header means the browser refuses to hand the response to the page.

    The request still executes server-side — CORS is a browser policy, not an
    authorization mechanism. Authorization is the bearer token, and that is
    tested elsewhere.
    """
    response = client.get("/health", headers={"Origin": "https://evil.example.com"})

    assert "access-control-allow-origin" not in response.headers


def test_origins_are_never_a_wildcard() -> None:
    """The configuration itself, not just its current value.

    A wildcard combined with a bearer token is the failure this guards: any
    site the user visits could read their tenant's data.
    """
    assert "*" not in settings.cors_origin_list
    assert settings.cors_origin_list, "an empty allowlist blocks the console entirely"


def test_credentials_are_not_allowed(client: TestClient) -> None:
    """The token travels in an Authorization header, not a cookie.

    allow_credentials=True would be meaningless here and would additionally
    make a future wildcard far more dangerous.
    """
    response = client.options(
        "/v1/me",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.headers.get("access-control-allow-credentials") != "true"


def test_preflight_permits_the_authorization_header(client: TestClient) -> None:
    """Without this the console cannot send a token at all."""
    response = client.options(
        "/v1/me",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    allowed_headers = response.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in allowed_headers


def test_write_methods_beyond_the_api_surface_are_not_advertised(
    client: TestClient,
) -> None:
    """PUT and PATCH are not used by any endpoint, so they are not offered.

    Not a security boundary — a browser refusing a method proves nothing about
    the server. It keeps the advertised surface honest.
    """
    response = client.options(
        "/v1/me",
        headers={"Origin": ALLOWED, "Access-Control-Request-Method": "GET"},
    )

    allowed = response.headers.get("access-control-allow-methods", "").upper()
    assert "PUT" not in allowed
    assert "PATCH" not in allowed
