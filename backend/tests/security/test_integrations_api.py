"""The integrations API.

Two of these tests are named in the roadmap's Phase 6 checklist, and they are
the two that matter:

  test_credentials_never_returned_by_api
  test_connection_endpoint_is_admin_only_and_rate_limited

The first is asserted against the raw response body rather than against a
parsed model. A test that checks `"credential" not in response.json()` would
pass while a credential sat inside `settings`, or under a different key, or
appended to a display name. Searching the serialised bytes for the actual
secret is the only version that cannot be satisfied by moving it.

The second is really three properties — authorization, throttling, and the
error text a failure returns — because "Test Connection" is a network probe
that an authenticated admin points wherever they like.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.core.config import settings
from app.core.security import issue_token
from app.main import app


def _database_available() -> bool:
    try:
        engine = create_engine(settings.database_url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


pytestmark = [
    pytest.mark.security,
    pytest.mark.skipif(not _database_available(), reason="no database reachable"),
]

TENANT = uuid.UUID("11220000-0000-0000-0000-000000000011")
OTHER = uuid.UUID("11220000-0000-0000-0000-0000000000ff")

# Distinctive enough that finding it anywhere in a response is unambiguous.
FIXTURE_SECRET = "zzq-unmistakable-secret-8842"  # noqa: S105 — a test fixture


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def tenants(engine: Engine) -> Iterator[None]:
    def _wipe() -> None:
        with engine.begin() as conn:
            params = {"a": TENANT, "b": OTHER}
            conn.execute(text("DELETE FROM connector WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM trace_span WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), params)

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO tenant (id, slug, name) VALUES
                  (:a, 'integ-test', 'Integ Test'), (:b, 'integ-other', 'Integ Other')
            """),
            {"a": TENANT, "b": OTHER},
        )
    yield
    _wipe()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_rate_limit() -> Iterator[None]:
    """A fixed-window limiter is shared state between tests.

    Without this, whichever test runs second inherits the first one's counter
    and fails for reasons that have nothing to do with what it asserts.
    """
    import contextlib

    from app.db.session import redis_client

    for tenant in (TENANT, OTHER):
        # Suppressed: if Redis is unreachable there is no counter to clear, and
        # the limiter's own fail-closed behaviour is what the tests then see.
        with contextlib.suppress(Exception):
            redis_client.delete(f"ratelimit:conn-test:{tenant}")
    yield


def _headers(
    *, admin: bool = True, tenant: uuid.UUID = TENANT, labels: tuple[str, ...] = ("analytics",)
) -> dict[str, str]:
    token = issue_token(
        tenant_id=tenant,
        user_id=uuid.uuid4(),
        email="admin@test" if admin else "user@test",
        roles=("admin",) if admin else ("reader",),
        allowed_labels=labels,
    )
    return {"Authorization": f"Bearer {token}"}


def _sql_body(
    slug: str = "analytics", credential: str | None = FIXTURE_SECRET
) -> dict[str, object]:
    body: dict[str, object] = {
        "kind": "sql",
        "slug": slug,
        "display_name": "Analytics warehouse",
        "required_labels": ["analytics"],
        "settings": {
            "host": settings.postgres_host,
            "port": settings.postgres_port,
            "database": "analytics",
            "username": "analytics_readonly",
            "allow_private": True,
            "allow_loopback": True,
        },
    }
    if credential is not None:
        body["credential"] = credential
    return body


# --- the roadmap's named test ------------------------------------------------


def test_credentials_never_returned_by_api(client: TestClient) -> None:
    """No endpoint returns a stored credential, in any form, to anyone.

    Asserted against the raw response TEXT, not a parsed field. A credential
    that moved into `settings`, or into a differently-named key, would satisfy
    a field-name check and fail this one — which is the point.
    """
    created = client.post("/v1/integrations", json=_sql_body(), headers=_headers())
    assert created.status_code == 201, created.text
    connector_id = created.json()["id"]

    responses = [
        created,
        client.get("/v1/integrations", headers=_headers()),
        client.get(f"/v1/integrations/{connector_id}", headers=_headers()),
        client.patch(
            f"/v1/integrations/{connector_id}",
            json={"display_name": "Renamed"},
            headers=_headers(),
        ),
    ]

    for response in responses:
        assert response.status_code < 300, response.text
        assert FIXTURE_SECRET not in response.text, (
            f"a credential leaked from {response.request.method} {response.request.url}"
        )
        # Nor a masked stand-in, which would imply the value is retrievable.
        # `has_credential` is expected and is the boolean the UI needs, so the
        # check is for a `credential` KEY rather than the substring — which
        # `has_credential` contains.
        payload = response.json()
        for item in payload if isinstance(payload, list) else [payload]:
            assert "credential" not in item, "a credential field was returned"

    # The UI still needs to know one is configured.
    assert created.json()["has_credential"] is True


def test_connection_endpoint_is_admin_only_and_rate_limited(client: TestClient) -> None:
    """The probe is admin-only and throttled.

    It points the platform's own network stack at an address the caller chose,
    which is SSRF with extra steps. Both controls are asserted here because
    either alone is insufficient: authorization without throttling lets one
    admin enumerate a subnet, and throttling without authorization lets anyone
    do it slowly.
    """
    created = client.post("/v1/integrations", json=_sql_body(), headers=_headers())
    connector_id = created.json()["id"]

    # A non-admin may not probe at all.
    forbidden = client.post(f"/v1/integrations/{connector_id}/test", headers=_headers(admin=False))
    assert forbidden.status_code == 403

    # An admin may, up to the limit. TEST_LIMIT is 5 per minute per tenant.
    from app.api.v1.integrations import TEST_LIMIT

    statuses = [
        client.post(f"/v1/integrations/{connector_id}/test", headers=_headers()).status_code
        for _ in range(TEST_LIMIT.limit + 2)
    ]

    assert statuses[: TEST_LIMIT.limit] == [200] * TEST_LIMIT.limit
    assert 429 in statuses[TEST_LIMIT.limit :], "the probe was never throttled"


def test_a_throttled_probe_says_when_to_retry(client: TestClient) -> None:
    """A 429 without Retry-After invites a tight retry loop."""
    created = client.post("/v1/integrations", json=_sql_body(), headers=_headers())
    connector_id = created.json()["id"]

    from app.api.v1.integrations import TEST_LIMIT

    last = None
    for _ in range(TEST_LIMIT.limit + 2):
        last = client.post(f"/v1/integrations/{connector_id}/test", headers=_headers())

    assert last is not None
    assert last.status_code == 429
    assert "Retry-After" in last.headers


# --- authorization -----------------------------------------------------------


def test_every_endpoint_requires_authentication(client: TestClient) -> None:
    assert client.get("/v1/integrations").status_code == 401
    assert client.post("/v1/integrations", json=_sql_body()).status_code == 401


def test_managing_integrations_requires_admin(client: TestClient) -> None:
    """A reader may use a connector; only an admin may configure one.

    Different privileges: using it is bounded by labels, configuring it means
    choosing what the platform connects to.
    """
    reader = _headers(admin=False)
    assert client.get("/v1/integrations", headers=reader).status_code == 403
    assert client.post("/v1/integrations", json=_sql_body(), headers=reader).status_code == 403


def test_another_tenants_connector_is_not_visible(client: TestClient) -> None:
    """404, not 403 — a different status would confirm it exists."""
    created = client.post("/v1/integrations", json=_sql_body(), headers=_headers())
    connector_id = created.json()["id"]

    other = client.get(f"/v1/integrations/{connector_id}", headers=_headers(tenant=OTHER))
    assert other.status_code == 404


def test_tenant_is_server_derived_not_taken_from_the_body(
    client: TestClient, engine: Engine
) -> None:
    """Invariant #1. A tenant_id in the body must be ignored entirely."""
    body = _sql_body()
    body["tenant_id"] = str(OTHER)

    created = client.post("/v1/integrations", json=body, headers=_headers())
    assert created.status_code == 201

    with engine.connect() as conn:
        owner = conn.execute(
            text("SELECT tenant_id FROM connector WHERE id = :id"),
            {"id": created.json()["id"]},
        ).scalar()
    assert owner == TENANT, "a request body chose its own tenant"


# --- validation --------------------------------------------------------------


def test_malformed_settings_are_rejected_at_creation(client: TestClient) -> None:
    """A connector missing a host should fail here, not at first query."""
    body = _sql_body()
    body["settings"] = {"host": "localhost"}  # no port, database, or username

    response = client.post("/v1/integrations", json=body, headers=_headers())
    assert response.status_code == 422


def test_an_unknown_kind_is_rejected(client: TestClient) -> None:
    body = _sql_body()
    body["kind"] = "mongodb"
    assert client.post("/v1/integrations", json=body, headers=_headers()).status_code == 422


def test_duplicate_slug_within_a_tenant_is_a_conflict(client: TestClient) -> None:
    assert client.post("/v1/integrations", json=_sql_body(), headers=_headers()).status_code == 201
    second = client.post("/v1/integrations", json=_sql_body(), headers=_headers())
    assert second.status_code == 409


def test_the_same_slug_is_free_in_another_tenant(client: TestClient) -> None:
    """Rejecting it would tell one tenant what another has configured."""
    assert client.post("/v1/integrations", json=_sql_body(), headers=_headers()).status_code == 201
    other = client.post("/v1/integrations", json=_sql_body(), headers=_headers(tenant=OTHER))
    assert other.status_code == 201


# --- updating ----------------------------------------------------------------


def test_omitting_the_credential_on_update_keeps_the_stored_one(
    client: TestClient, engine: Engine
) -> None:
    """The usual case, and it has to work: the credential cannot be read back,
    so an editor has nothing to re-submit."""
    created = client.post("/v1/integrations", json=_sql_body(), headers=_headers())
    connector_id = created.json()["id"]

    with engine.connect() as conn:
        before = conn.execute(
            text("SELECT credential FROM connector WHERE id = :id"), {"id": connector_id}
        ).scalar()

    client.patch(
        f"/v1/integrations/{connector_id}",
        json={"display_name": "Renamed warehouse"},
        headers=_headers(),
    )

    with engine.connect() as conn:
        after = conn.execute(
            text("SELECT credential FROM connector WHERE id = :id"), {"id": connector_id}
        ).scalar()

    assert after == before
    assert after is not None


def test_a_new_credential_replaces_the_old_ciphertext(client: TestClient, engine: Engine) -> None:
    created = client.post("/v1/integrations", json=_sql_body(), headers=_headers())
    connector_id = created.json()["id"]

    with engine.connect() as conn:
        before = conn.execute(
            text("SELECT credential FROM connector WHERE id = :id"), {"id": connector_id}
        ).scalar()

    client.patch(
        f"/v1/integrations/{connector_id}",
        json={"credential": "a-completely-different-value"},
        headers=_headers(),
    )

    with engine.connect() as conn:
        after = conn.execute(
            text("SELECT credential FROM connector WHERE id = :id"), {"id": connector_id}
        ).scalar()

    assert after != before
    assert "a-completely-different-value" not in (after or ""), "stored in plaintext"


def test_disabling_keeps_the_configuration(client: TestClient) -> None:
    """Disable is not delete. Turning a connector off for an hour should not
    destroy its credential."""
    created = client.post("/v1/integrations", json=_sql_body(), headers=_headers())
    connector_id = created.json()["id"]

    patched = client.patch(
        f"/v1/integrations/{connector_id}", json={"enabled": False}, headers=_headers()
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert patched.json()["has_credential"] is True


# --- the probe itself --------------------------------------------------------


def test_a_working_connector_reports_ok(client: TestClient) -> None:
    """The analytics database is reachable in this environment, so a correctly
    configured connector must actually connect — otherwise the endpoint proves
    nothing."""
    created = client.post(
        "/v1/integrations",
        json=_sql_body(credential=settings.postgres_readonly_password),
        headers=_headers(),
    )
    connector_id = created.json()["id"]

    result = client.post(f"/v1/integrations/{connector_id}/test", headers=_headers())
    assert result.status_code == 200
    assert result.json()["ok"] is True, result.text


def test_a_blocked_address_fails_without_naming_it(client: TestClient) -> None:
    """Egress is checked before a socket opens, and the error names a class.

    169.254.169.254 is cloud metadata. A probe that reported "connection
    refused" versus "timed out" for such an address would turn a failed test
    into a working port scanner.
    """
    body = _sql_body(slug="metadata")
    body["settings"] = {
        "host": "169.254.169.254",
        "port": 80,
        "database": "x",
        "username": "y",
    }
    created = client.post("/v1/integrations", json=body, headers=_headers())
    assert created.status_code == 201

    result = client.post(f"/v1/integrations/{created.json()['id']}/test", headers=_headers())
    assert result.status_code == 200
    payload = result.json()
    assert payload["ok"] is False
    assert "egress" in (payload["error"] or "").lower()


def test_a_failed_probe_does_not_echo_upstream_error_text(client: TestClient) -> None:
    """An error message carrying a hostname or a driver version is disclosure."""
    body = _sql_body(slug="wrong-port")
    body["settings"] = {
        "host": settings.postgres_host,
        # Nothing listens here.
        "port": 59999,
        "database": "analytics",
        "username": "analytics_readonly",
        "allow_private": True,
        "allow_loopback": True,
    }
    created = client.post("/v1/integrations", json=body, headers=_headers())
    result = client.post(f"/v1/integrations/{created.json()['id']}/test", headers=_headers())

    error = (result.json()["error"] or "").lower()
    assert error, "a failure must say something"
    assert "59999" not in error
    assert "psycopg" not in error
    assert "postgres" not in error


def test_the_probe_result_is_recorded(client: TestClient) -> None:
    """So an operator can see health without re-probing — the probe is
    rate-limited precisely because it should not be run casually."""
    created = client.post(
        "/v1/integrations",
        json=_sql_body(credential=settings.postgres_readonly_password),
        headers=_headers(),
    )
    connector_id = created.json()["id"]
    client.post(f"/v1/integrations/{connector_id}/test", headers=_headers())

    fetched = client.get(f"/v1/integrations/{connector_id}", headers=_headers()).json()
    assert fetched["last_tested_at"] is not None
    assert fetched["last_test_ok"] is True


# --- deletion ----------------------------------------------------------------


def test_delete_removes_the_connector(client: TestClient) -> None:
    created = client.post("/v1/integrations", json=_sql_body(), headers=_headers())
    connector_id = created.json()["id"]

    assert client.delete(f"/v1/integrations/{connector_id}", headers=_headers()).status_code == 204
    assert client.get(f"/v1/integrations/{connector_id}", headers=_headers()).status_code == 404


def test_deleting_another_tenants_connector_is_a_404(client: TestClient) -> None:
    created = client.post("/v1/integrations", json=_sql_body(), headers=_headers())
    connector_id = created.json()["id"]

    assert (
        client.delete(
            f"/v1/integrations/{connector_id}", headers=_headers(tenant=OTHER)
        ).status_code
        == 404
    )
