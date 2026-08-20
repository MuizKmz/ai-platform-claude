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


def _methods_the_api_serves() -> set[str]:
    """Every HTTP method some endpoint actually serves.

    Read from the OpenAPI schema rather than by walking `app.routes`: this
    FastAPI version wraps included routers in objects whose `methods` is None,
    so a naive walk reports only the docs endpoints — GET and HEAD — and every
    assertion built on it passes vacuously. That is exactly the mistake this
    file exists to catch, and it was made here first.
    """
    from app.main import app

    spec = app.openapi()
    return {method.upper() for path in spec["paths"].values() for method in path}


def _advertised_methods(client: TestClient) -> set[str]:
    response = client.options(
        "/v1/me",
        headers={"Origin": ALLOWED, "Access-Control-Request-Method": "GET"},
    )
    return {
        m.strip().upper()
        for m in response.headers.get("access-control-allow-methods", "").split(",")
        if m.strip()
    }


def test_every_method_the_api_uses_is_advertised(client: TestClient) -> None:
    """The allowlist must cover every verb some endpoint serves.

    Derived rather than hardcoded, because the hardcoded version of this test
    caused the bug it was meant to prevent: it asserted PATCH was absent, which
    was correct when written since no endpoint used one. The integrations API
    then added PATCH and the allowlist was not updated.

    The failure mode is invisible from the server side. The browser's PREFLIGHT
    gets 400 "Disallowed CORS method" and the real request is never sent, so
    curl works perfectly, every test passes, and the feature is broken only in
    a browser.
    """
    missing = _methods_the_api_serves() - _advertised_methods(client)
    assert not missing, (
        f"endpoints serve {sorted(missing)} but CORS does not advertise it — "
        "the browser preflight will 400 while curl succeeds"
    )


def test_methods_no_endpoint_serves_are_not_advertised(client: TestClient) -> None:
    """The complement: the advertised surface stays honest.

    Not a security boundary — a browser refusing a method proves nothing about
    the server, and the endpoint's own routing is what actually refuses. This
    keeps the allowlist from drifting into a wildcard by accretion.

    OPTIONS is permitted regardless: CORS requires it and no endpoint declares
    it.
    """
    extra = _advertised_methods(client) - _methods_the_api_serves() - {"OPTIONS"}
    assert not extra, f"CORS advertises {sorted(extra)}, which no endpoint serves"
