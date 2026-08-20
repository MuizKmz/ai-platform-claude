"""Traces and audit: admin-only, and tenant-scoped by the database.

These endpoints expose records ABOUT users rather than records FOR them. A
trace names which tenant made a request and what it cost; an audit row carries
the SQL somebody ran. An ordinary reader seeing every colleague's queries is a
surveillance surface nobody asked for, so both are admin-gated — and the tests
below check the gate as well as the isolation beneath it.
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

TENANT = uuid.UUID("6f6f0000-0000-0000-0000-00000000006f")
OTHER = uuid.UUID("6f6f0000-0000-0000-0000-0000000000ff")
USER = uuid.UUID("6f6f0000-0000-0000-0000-0000000000aa")

TRACE_ID = "aaaabbbbccccddddeeeeffff00001111"
OTHER_TRACE_ID = "99998888777766665555444433332222"


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def seed(engine: Engine) -> Iterator[None]:
    def _wipe() -> None:
        with engine.begin() as conn:
            params = {"a": TENANT, "b": OTHER}
            conn.execute(text("DELETE FROM connector_audit WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM trace_span WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), params)

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO tenant (id, slug, name) VALUES
                  (:a, 'obs-test', 'Obs Test'), (:b, 'obs-other', 'Obs Other')
            """),
            {"a": TENANT, "b": OTHER},
        )
        # Two spans of one trace for our tenant.
        for name, duration in (("knowledge.retrieve", 42.0), ("llm.generate", 810.5)):
            conn.execute(
                text("""
                    INSERT INTO trace_span
                      (id, trace_id, span_id, name, tenant_id, duration_ms, status, attributes)
                    VALUES (:id, :tid, :sid, :name, :t, :ms, 'ok', '{}'::json)
                """),
                {
                    "id": uuid.uuid4(),
                    "tid": TRACE_ID,
                    "sid": uuid.uuid4().hex[:16],
                    "name": name,
                    "t": TENANT,
                    "ms": duration,
                },
            )
        # And one belonging to somebody else entirely.
        conn.execute(
            text("""
                INSERT INTO trace_span
                  (id, trace_id, span_id, name, tenant_id, duration_ms, status, attributes)
                VALUES (:id, :tid, :sid, 'knowledge.retrieve', :t, 5.0, 'ok', '{}'::json)
            """),
            {
                "id": uuid.uuid4(),
                "tid": OTHER_TRACE_ID,
                "sid": uuid.uuid4().hex[:16],
                "t": OTHER,
            },
        )
        # One allowed and one denied audit row.
        for allowed, sql, reason in (
            (True, "SELECT count(*) FROM curated.v_orders", None),
            (False, "DELETE FROM curated.v_orders", "DELETE is not permitted."),
        ):
            conn.execute(
                text("""
                    INSERT INTO connector_audit
                      (id, tenant_id, user_id, user_email, connector_id, sql,
                       question, allowed, denial_reason)
                    VALUES (:id, :t, :u, 'analyst@test', 'analytics', :sql,
                            'how many orders', :allowed, :reason)
                """),
                {
                    "id": uuid.uuid4(),
                    "t": TENANT,
                    "u": USER,
                    "sql": sql,
                    "allowed": allowed,
                    "reason": reason,
                },
            )
        conn.execute(
            text("""
                INSERT INTO connector_audit
                  (id, tenant_id, user_id, user_email, connector_id, sql, allowed)
                VALUES (:id, :t, :u, 'other@test', 'analytics', 'SELECT 1', true)
            """),
            {"id": uuid.uuid4(), "t": OTHER, "u": uuid.uuid4()},
        )
    yield
    _wipe()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _headers(roles: tuple[str, ...] = ("admin",), tenant: uuid.UUID = TENANT) -> dict[str, str]:
    token = issue_token(
        tenant_id=tenant,
        user_id=USER,
        email="obs@test",
        roles=roles,
        allowed_labels=("public",),
    )
    return {"Authorization": f"Bearer {token}"}


# --- authorization ------------------------------------------------------------


def test_traces_require_auth(client: TestClient) -> None:
    assert client.get("/v1/traces").status_code == 401


def test_audit_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/audit").status_code == 401


def test_traces_require_the_admin_role(client: TestClient) -> None:
    """A trace names what a colleague asked and what it cost."""
    assert client.get("/v1/traces", headers=_headers(("reader",))).status_code == 403


def test_audit_requires_the_admin_role(client: TestClient) -> None:
    """An audit row carries the SQL somebody ran."""
    assert client.get("/v1/audit", headers=_headers(("reader",))).status_code == 403


# --- tenant isolation ---------------------------------------------------------


def test_traces_are_tenant_scoped(client: TestClient) -> None:
    """Isolation comes from RLS, not from a predicate in the endpoint.

    The query has no tenant clause at all — which is the point. Forgetting one
    returns nothing rather than everything.
    """
    body = client.get("/v1/traces", headers=_headers()).json()

    trace_ids = {trace["trace_id"] for trace in body}
    assert TRACE_ID in trace_ids
    assert OTHER_TRACE_ID not in trace_ids


def test_audit_is_tenant_scoped(client: TestClient) -> None:
    body = client.get("/v1/audit", headers=_headers()).json()

    assert body
    assert all(row["user_email"] != "other@test" for row in body)


def test_an_admin_of_another_tenant_sees_nothing_of_ours(client: TestClient) -> None:
    """Admin is a role within a tenant, never across them."""
    body = client.get("/v1/traces", headers=_headers(("admin",), OTHER)).json()

    assert {t["trace_id"] for t in body} == {OTHER_TRACE_ID}


# --- shape --------------------------------------------------------------------


def test_spans_are_grouped_into_one_trace(client: TestClient) -> None:
    """A request is one row in the list, not one row per span."""
    body = client.get("/v1/traces", headers=_headers()).json()

    trace = next(t for t in body if t["trace_id"] == TRACE_ID)
    assert trace["span_count"] == 2
    assert {s["name"] for s in trace["spans"]} == {
        "knowledge.retrieve",
        "llm.generate",
    }
    assert trace["total_duration_ms"] == pytest.approx(852.5)


def test_denied_only_filters_to_refusals(client: TestClient) -> None:
    """The rows worth reading. A run of refusals is what a bypass attempt looks
    like, and it is invisible among successful queries."""
    body = client.get("/v1/audit?denied_only=true", headers=_headers()).json()

    assert body
    assert all(row["allowed"] is False for row in body)
    assert any("DELETE" in row["sql"] for row in body)


def test_audit_records_the_refused_sql(client: TestClient) -> None:
    """A denied row without its SQL cannot answer what was attempted."""
    body = client.get("/v1/audit?denied_only=true", headers=_headers()).json()

    denied = body[0]
    assert denied["sql"]
    assert denied["denial_reason"]


def test_limit_is_capped(client: TestClient) -> None:
    """An unbounded history is a bulk-export route, as with query results."""
    assert client.get("/v1/traces?limit=100000", headers=_headers()).status_code == 422
    assert client.get("/v1/audit?limit=100000", headers=_headers()).status_code == 422
