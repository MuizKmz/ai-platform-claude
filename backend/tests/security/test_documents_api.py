"""The documents API: what a caller may list, and who may delete."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

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

TENANT = uuid.UUID("2b2b0000-0000-0000-0000-00000000002b")
USER = uuid.UUID("2b2b0000-0000-0000-0000-0000000000bb")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


def _seed(conn: object, title: str, labels: list[str], superseded: bool = False) -> uuid.UUID:
    provider = FakeEmbeddings()
    (vector,) = provider.embed([f"content of {title}"])
    doc_id = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text("""
            INSERT INTO document
              (id, tenant_id, title, source_path, content_hash, labels, superseded_at)
            VALUES (:id, :t, :title, :src, :hash, :labels, :sup)
        """),
        {
            "id": doc_id,
            "t": TENANT,
            "title": title,
            "src": f"/{title}",
            "hash": uuid.uuid4().hex,
            "labels": labels,
            "sup": "2026-01-01T00:00:00Z" if superseded else None,
        },
    )
    conn.execute(  # type: ignore[attr-defined]
        text("""
            INSERT INTO chunk
              (id, tenant_id, document_id, ordinal, content, embedding, embedding_model,
               embedding_dim)
            VALUES (:id, :t, :d, 0, :content, :emb, :model, :dim)
        """),
        {
            "id": uuid.uuid4(),
            "t": TENANT,
            "d": doc_id,
            "content": f"content of {title}",
            "emb": str(vector),
            "model": provider.model,
            "dim": provider.dimension,
        },
    )
    return doc_id


@pytest.fixture(autouse=True)
def seed(engine: Engine) -> Iterator[dict[str, uuid.UUID]]:
    def _wipe() -> None:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM chunk WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM document WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": TENANT})

    _wipe()
    ids: dict[str, uuid.UUID] = {}
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenant (id, slug, name) VALUES (:t, 'docs-test', 'Docs')"),
            {"t": TENANT},
        )
        ids["public"] = _seed(conn, "handbook", ["public"])
        ids["finance"] = _seed(conn, "salaries", ["finance"])
        ids["old"] = _seed(conn, "old-handbook", ["public"], superseded=True)
    yield ids
    _wipe()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _token(labels: tuple[str, ...], roles: tuple[str, ...] = ("reader",)) -> str:
    return issue_token(
        tenant_id=TENANT, user_id=USER, email="docs@test", roles=roles, allowed_labels=labels
    )


def _headers(
    labels: tuple[str, ...] = ("public",), roles: tuple[str, ...] = ("reader",)
) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(labels, roles)}"}


# --- listing ------------------------------------------------------------------


def test_listing_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/documents").status_code == 401


def test_listing_is_filtered_by_label(client: TestClient) -> None:
    """A user without `finance` must not learn that a finance document exists.

    Listing endpoints are an easy place to disclose the existence of data whose
    contents are correctly protected everywhere else.
    """
    body = client.get("/v1/documents", headers=_headers(("public",))).json()

    titles = {d["title"] for d in body}
    assert "handbook" in titles
    assert "salaries" not in titles


def test_listing_excludes_superseded_by_default(client: TestClient) -> None:
    body = client.get("/v1/documents", headers=_headers(("public",))).json()

    assert {d["title"] for d in body} == {"handbook"}


def test_superseded_can_be_listed_explicitly(client: TestClient) -> None:
    """Retained for audit, so they must be reachable — just not by default."""
    body = client.get("/v1/documents?include_superseded=true", headers=_headers(("public",))).json()

    titles = {d["title"] for d in body}
    assert titles == {"handbook", "old-handbook"}


# --- deletion -----------------------------------------------------------------


def test_delete_requires_the_admin_role(client: TestClient, seed: dict[str, uuid.UUID]) -> None:
    """Reading a document must not imply the authority to destroy it."""
    response = client.delete(
        f"/v1/documents/{seed['public']}", headers=_headers(("public",), ("reader",))
    )

    assert response.status_code == 403


def test_admin_can_delete_and_chunks_go_with_it(
    client: TestClient, seed: dict[str, uuid.UUID], engine: Engine
) -> None:
    response = client.delete(
        f"/v1/documents/{seed['public']}", headers=_headers(("public",), ("admin",))
    )

    assert response.status_code == 200
    assert response.json() == {"documents_deleted": 1, "chunks_deleted": 1}

    with engine.connect() as conn:
        remaining = conn.execute(
            text("SELECT count(*) FROM chunk WHERE document_id = :d"), {"d": seed["public"]}
        ).scalar()
    assert remaining == 0


def test_deleting_an_invisible_document_is_404_not_403(
    client: TestClient, seed: dict[str, uuid.UUID]
) -> None:
    """An admin without the `finance` label gets the same answer as for a
    document that does not exist. A 403 would confirm it exists."""
    response = client.delete(
        f"/v1/documents/{seed['finance']}", headers=_headers(("public",), ("admin",))
    )

    assert response.status_code == 404


def test_cannot_delete_another_tenants_document(
    client: TestClient, seed: dict[str, uuid.UUID], engine: Engine
) -> None:
    """A token for a different tenant must not reach this row, even with the id."""
    other_token = issue_token(
        tenant_id=uuid.uuid4(),
        user_id=USER,
        email="other@test",
        roles=("admin",),
        allowed_labels=("public",),
    )

    response = client.delete(
        f"/v1/documents/{seed['public']}", headers={"Authorization": f"Bearer {other_token}"}
    )

    assert response.status_code == 404
    with engine.connect() as conn:
        still_there = conn.execute(
            text("SELECT count(*) FROM document WHERE id = :d"), {"d": seed["public"]}
        ).scalar()
    assert still_there == 1
