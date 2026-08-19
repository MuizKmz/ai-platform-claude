"""Retrieval authorization: the filter runs before ranking, not after.

The roadmap's four retrieval security tests live here. Each asserts a property
that is cheap now and catastrophic to retrofit.
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.knowledge.embedding import FakeEmbeddings
from app.knowledge.retrieval import search


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

TENANT_A = uuid.UUID("dddd0000-0000-0000-0000-00000000000a")
TENANT_B = uuid.UUID("dddd0000-0000-0000-0000-00000000000b")

# Identical in both tenants, so isolation cannot be an artefact of differing text.
SHARED = "Refunds are processed within five business days of approval."


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


def _insert_doc(
    conn: object, tenant_id: uuid.UUID, title: str, content: str, labels: list[str]
) -> None:
    provider = FakeEmbeddings()
    (vector,) = provider.embed([content])
    doc_id = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text("""
            INSERT INTO document (id, tenant_id, title, source_path, content_hash, labels)
            VALUES (:id, :t, :title, :src, :hash, :labels)
        """),
        {
            "id": doc_id,
            "t": tenant_id,
            "title": title,
            "src": f"/{title}",
            "hash": uuid.uuid4().hex,
            "labels": labels,
        },
    )
    conn.execute(  # type: ignore[attr-defined]
        text("""
            INSERT INTO chunk
              (id, tenant_id, document_id, ordinal, content, embedding, embedding_model,
               embedding_dim)
            VALUES (:id, :t, :doc, 0, :content, :emb, :model, :dim)
        """),
        {
            "id": uuid.uuid4(),
            "t": tenant_id,
            "doc": doc_id,
            "content": content,
            "emb": str(vector),
            "model": provider.model,
            "dim": provider.dimension,
        },
    )


@pytest.fixture(autouse=True)
def seed(engine: Engine) -> Iterator[None]:
    def _wipe() -> None:
        with engine.begin() as conn:
            params = {"a": TENANT_A, "b": TENANT_B}
            conn.execute(text("DELETE FROM chunk WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM document WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), params)

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO tenant (id, slug, name) VALUES
                  (:a, 'search-test-a', 'Search A'), (:b, 'search-test-b', 'Search B')
            """),
            {"a": TENANT_A, "b": TENANT_B},
        )
        # Same text in both tenants.
        _insert_doc(conn, TENANT_A, "shared-a", SHARED, ["public"])
        _insert_doc(conn, TENANT_B, "shared-b", SHARED, ["public"])
        # Label-restricted, and unlabelled.
        _insert_doc(conn, TENANT_A, "salaries", "The CEO salary is 450000 per year.", ["finance"])
        _insert_doc(conn, TENANT_A, "orphan", "This document carries no labels at all.", [])
        # Enough public chunks that a top-5 can be filled without the finance one.
        for i in range(6):
            _insert_doc(
                conn, TENANT_A, f"filler-{i}", f"Public filler document number {i}.", ["public"]
            )
    yield
    _wipe()


def _search(engine: Engine, tenant_id: uuid.UUID, labels: tuple[str, ...], q: str, limit: int = 5):
    with Session(engine) as session:
        return search(
            session,
            query=q,
            tenant_id=tenant_id,
            allowed_labels=labels,
            provider=FakeEmbeddings(),
            limit=limit,
        )


def test_cannot_retrieve_other_tenant_chunks(engine: Engine) -> None:
    """Identical text in two tenants; an exact-match query never crosses over."""
    hits = _search(engine, TENANT_A, ("public",), SHARED, limit=10)

    assert hits, "expected the tenant's own copy to be retrieved"
    assert all(
        h.document_title == "shared-a" or h.document_title.startswith("filler") for h in hits
    )
    assert not any(h.document_title == "shared-b" for h in hits)


def test_unlabeled_document_is_invisible(engine: Engine) -> None:
    """Default-deny: no labels means visible to nobody, not visible to everybody."""
    hits = _search(
        engine, TENANT_A, ("public", "finance"), "This document carries no labels at all.", limit=10
    )

    assert not any(h.document_title == "orphan" for h in hits)


def test_label_filter_hides_unauthorized_documents(engine: Engine) -> None:
    """The same query returns the finance chunk only to a caller holding 'finance'."""
    query = "The CEO salary is 450000 per year."

    with_finance = _search(engine, TENANT_A, ("public", "finance"), query, limit=5)
    without_finance = _search(engine, TENANT_A, ("public",), query, limit=5)

    assert any(h.document_title == "salaries" for h in with_finance)
    assert not any(h.document_title == "salaries" for h in without_finance)


def test_filter_is_in_sql_not_post_hoc(engine: Engine) -> None:
    """A restrictive label set still returns a FULL top-k of permitted rows.

    Post-filtering would return 5 minus whatever was removed — the caller would
    silently get fewer results than they asked for, with nothing to say why.
    """
    hits = _search(engine, TENANT_A, ("public",), "Public filler document", limit=5)

    assert len(hits) == 5, f"expected a full top-5 of permitted chunks, got {len(hits)}"
    assert not any(h.document_title in {"salaries", "orphan"} for h in hits)


def test_no_labels_returns_nothing(engine: Engine) -> None:
    """A caller with no labels configured sees nothing, rather than everything."""
    assert _search(engine, TENANT_A, (), SHARED, limit=10) == []


def test_empty_query_returns_nothing(engine: Engine) -> None:
    assert _search(engine, TENANT_A, ("public",), "   ", limit=5) == []


def test_limit_is_capped(engine: Engine) -> None:
    """An unbounded limit is a denial-of-service vector and a data-dump vector."""
    hits = _search(engine, TENANT_A, ("public",), SHARED, limit=10_000)

    assert len(hits) <= 50
