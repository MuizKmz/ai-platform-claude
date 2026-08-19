"""The search endpoint itself: authentication, and what the response reveals."""

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.api.v1 import search as search_module
from app.core.config import settings
from app.core.security import issue_token
from app.knowledge.embedding import FakeEmbeddings
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

TENANT = uuid.UUID("eeee0000-0000-0000-0000-00000000000a")
USER = uuid.UUID("eeee0000-0000-0000-0000-0000000000ff")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def seed(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The endpoint must not call OpenAI during tests.
    monkeypatch.setattr(search_module, "get_embedding_provider", FakeEmbeddings)

    def _wipe() -> None:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM trace_span WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM chunk WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM document WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": TENANT})

    _wipe()
    provider = FakeEmbeddings()
    (vector,) = provider.embed(["Refunds take five business days."])
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenant (id, slug, name) VALUES (:t, 'api-test', 'API Test')"),
            {"t": TENANT},
        )
        doc_id = uuid.uuid4()
        conn.execute(
            text("""
                INSERT INTO document (id, tenant_id, title, source_path, content_hash, labels)
                VALUES (:id, :t, 'refunds', '/r.md', :h, '{public}')
            """),
            {"id": doc_id, "t": TENANT, "h": uuid.uuid4().hex},
        )
        conn.execute(
            text("""
                INSERT INTO chunk
                  (id, tenant_id, document_id, ordinal, content, embedding, embedding_model,
                   embedding_dim)
                VALUES (:id, :t, :d, 0, 'Refunds take five business days.', :e, :m, :dim)
            """),
            {
                "id": uuid.uuid4(),
                "t": TENANT,
                "d": doc_id,
                "e": str(vector),
                "m": provider.model,
                "dim": provider.dimension,
            },
        )
    yield
    _wipe()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _token(labels: tuple[str, ...] = ("public",)) -> str:
    return issue_token(
        tenant_id=TENANT, user_id=USER, email="api@test", roles=("reader",), allowed_labels=labels
    )


def test_search_requires_auth(client: TestClient) -> None:
    """Roadmap test: 401 without a valid token."""
    assert client.get("/v1/search?q=refund").status_code == 401
    bad = client.get("/v1/search?q=refund", headers={"Authorization": "Bearer bad"})
    assert bad.status_code == 401


def test_search_returns_permitted_chunks(client: TestClient) -> None:
    response = client.get(
        "/v1/search?q=Refunds take five business days.",
        headers={"Authorization": f"Bearer {_token()}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["results"], "expected a hit for an exact-match query"
    assert body["results"][0]["document_title"] == "refunds"


def test_response_echoes_the_callers_own_scope(client: TestClient) -> None:
    """filtered_by discloses the caller's OWN authority, which they already hold.

    It must never describe what was filtered away — that would leak the existence
    of data they may not see.
    """
    response = client.get(
        "/v1/search?q=refund", headers={"Authorization": f"Bearer {_token(('public',))}"}
    )

    body = response.json()
    assert body["filtered_by"]["tenant_id"] == str(TENANT)
    assert body["filtered_by"]["labels"] == ["public"]
    assert "excluded" not in str(body).lower()


def test_search_cannot_be_pointed_at_another_tenant(client: TestClient) -> None:
    """There is no tenant parameter; supplying one changes nothing."""
    other = uuid.uuid4()

    response = client.get(
        f"/v1/search?q=refund&tenant_id={other}",
        headers={"Authorization": f"Bearer {_token()}", "X-Tenant-Id": str(other)},
    )

    assert response.status_code == 200
    assert response.json()["filtered_by"]["tenant_id"] == str(TENANT)


def test_every_search_persists_a_trace_span(client: TestClient, engine: Engine) -> None:
    """Invariant #6: a trace per retrieval call, with a client-visible trace id."""
    response = client.get("/v1/search?q=refund", headers={"Authorization": f"Bearer {_token()}"})
    trace_id = response.json()["trace_id"]

    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT name, tenant_id, duration_ms, status, attributes
                FROM trace_span WHERE trace_id = :tid
            """),
            {"tid": trace_id},
        ).one()

    assert row.name == "knowledge.search"
    assert row.tenant_id == TENANT
    assert row.duration_ms > 0
    assert row.status == "ok"


def test_trace_does_not_record_query_text_or_content(client: TestClient, engine: Engine) -> None:
    """A span table is a second copy of tenant data that nobody reviews.

    Only counts and lengths belong there.
    """
    marker = "extremely-distinctive-query-string"
    response = client.get(f"/v1/search?q={marker}", headers={"Authorization": f"Bearer {_token()}"})
    trace_id = response.json()["trace_id"]

    with engine.connect() as conn:
        attributes = conn.execute(
            text("SELECT attributes FROM trace_span WHERE trace_id = :tid"), {"tid": trace_id}
        ).scalar()

    assert marker not in str(attributes)
    assert "Refunds take five" not in str(attributes)


def test_limit_beyond_the_cap_is_rejected(client: TestClient) -> None:
    """An unbounded limit is both a DoS and a bulk-export vector."""
    response = client.get(
        "/v1/search?q=refund&limit=100000", headers={"Authorization": f"Bearer {_token()}"}
    )

    assert response.status_code == 422
