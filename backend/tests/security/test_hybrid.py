"""Hybrid retrieval: fusion mechanics, and the authorization it must not lose.

Hybrid search adds a SECOND path to the data. That is the security risk worth
testing: a keyword retriever that skipped the tenant or label predicates would be
a way in that the vector retriever's WHERE clause was protecting.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.knowledge.embedding import FakeEmbeddings
from app.knowledge.hybrid import RRF_K, hybrid_search


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

TENANT_A = uuid.UUID("3c3c0000-0000-0000-0000-00000000003a")
TENANT_B = uuid.UUID("3c3c0000-0000-0000-0000-00000000003b")

PART = "Part QN-1183-A is a thermal sensor array with a 28 day lead time."
REFUND = "Refunds are processed within five business days of approval."
RESTRICTED = "Executive compensation is reviewed annually by the board."


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


def _seed_doc(
    conn: object, tenant_id: uuid.UUID, title: str, content: str, labels: list[str]
) -> uuid.UUID:
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
            VALUES (:id, :t, :d, 0, :content, :emb, :model, :dim)
        """),
        {
            "id": uuid.uuid4(),
            "t": tenant_id,
            "d": doc_id,
            "content": content,
            "emb": str(vector),
            "model": provider.model,
            "dim": provider.dimension,
        },
    )
    return doc_id


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
                  (:a, 'hybrid-a', 'Hybrid A'), (:b, 'hybrid-b', 'Hybrid B')
            """),
            {"a": TENANT_A, "b": TENANT_B},
        )
        _seed_doc(conn, TENANT_A, "parts", PART, ["public"])
        _seed_doc(conn, TENANT_A, "refunds", REFUND, ["public"])
        _seed_doc(conn, TENANT_A, "board", RESTRICTED, ["restricted"])
        _seed_doc(conn, TENANT_A, "orphan", "This document has no labels.", [])
        # Byte-identical content in the other tenant.
        _seed_doc(conn, TENANT_B, "parts", PART, ["public"])
    yield
    _wipe()


def _search(
    engine: Engine,
    query: str,
    tenant_id: uuid.UUID = TENANT_A,
    labels: tuple[str, ...] = ("public",),
    limit: int = 5,
) -> list:
    with Session(engine) as session:
        return hybrid_search(
            session,
            query=query,
            tenant_id=tenant_id,
            allowed_labels=labels,
            provider=FakeEmbeddings(),
            limit=limit,
        )


# --- authorization: the reason these are security tests -----------------------


def test_hybrid_cannot_cross_tenants(engine: Engine) -> None:
    """Identical content in both tenants; an exact-match query stays home.

    The keyword retriever is a second path to the data, and it carries the same
    tenant predicate as the vector one.
    """
    results = _search(engine, PART, tenant_id=TENANT_A, limit=10)

    assert results
    with Session(engine) as session:
        for fused in results:
            owner = session.execute(
                text("SELECT tenant_id FROM chunk WHERE id = :id"),
                {"id": fused.hit.chunk_id},
            ).scalar()
            assert owner == TENANT_A


def test_hybrid_respects_labels(engine: Engine) -> None:
    """A keyword match on restricted content must still be refused.

    The literal words of the query appear in the restricted document, so this
    fails loudly if the label predicate is missing from the keyword branch.
    """
    results = _search(engine, "executive compensation reviewed annually", labels=("public",))

    assert not any(f.hit.document_title == "board" for f in results)


def test_hybrid_honours_default_deny(engine: Engine) -> None:
    """An unlabelled document is invisible to both retrievers."""
    results = _search(engine, "This document has no labels.", limit=10)

    assert not any(f.hit.document_title == "orphan" for f in results)


def test_no_labels_returns_nothing(engine: Engine) -> None:
    assert _search(engine, PART, labels=()) == []


def test_superseded_documents_are_excluded(engine: Engine) -> None:
    """Tombstoned revisions must not return through the keyword path either."""
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE document SET superseded_at = now()
                WHERE tenant_id = :t AND title = 'parts'
            """),
            {"t": TENANT_A},
        )

    results = _search(engine, PART, limit=10)

    assert not any(f.hit.document_title == "parts" for f in results)


# --- fusion mechanics ---------------------------------------------------------


def test_fusion_reports_both_ranks(engine: Engine) -> None:
    """The ranks are kept for debugging: "the keyword side never saw this" and
    "both sides ranked it 20th" are different problems with different fixes."""
    results = _search(engine, REFUND, limit=5)

    assert results
    top = results[0]
    assert top.vector_rank is not None or top.keyword_rank is not None


def test_a_chunk_found_by_both_outranks_one_found_by_either(engine: Engine) -> None:
    """The point of fusion: agreement between retrievers is evidence."""
    results = _search(engine, PART, limit=10)

    both = [f for f in results if f.vector_rank and f.keyword_rank]
    one = [f for f in results if not (f.vector_rank and f.keyword_rank)]

    if both and one:
        assert min(f.score for f in both) > max(f.score for f in one)


def test_scores_follow_the_rrf_formula(engine: Engine) -> None:
    """Score is the sum over retrievers of weight / (k + rank), not a blend of
    raw similarity numbers — a cosine distance and a ts_rank are not comparable
    quantities, and normalising them requires a weighting nobody measured."""
    from app.knowledge.hybrid import KEYWORD_WEIGHT

    results = _search(engine, REFUND, limit=5)

    for fused in results:
        expected = 0.0
        if fused.vector_rank is not None:
            expected += 1.0 / (RRF_K + fused.vector_rank)
        if fused.keyword_rank is not None:
            expected += KEYWORD_WEIGHT / (RRF_K + fused.keyword_rank)
        assert fused.score == pytest.approx(expected)


def test_results_are_stable_across_runs(engine: Engine) -> None:
    """Ties are broken deterministically. An eval that reorders equal-scoring
    hits between runs reports noise as movement."""
    first = [f.hit.chunk_id for f in _search(engine, PART, limit=5)]
    second = [f.hit.chunk_id for f in _search(engine, PART, limit=5)]

    assert first == second


def test_empty_query_returns_nothing(engine: Engine) -> None:
    assert _search(engine, "   ") == []


def test_malformed_query_does_not_raise(engine: Engine) -> None:
    """Users paste odd things. A stray bracket must not become a 500 — which is
    why the query is built from lexemes rather than parsed as raw tsquery text.
    """
    for query in ("((", "a & | b", "'unclosed", "!@#$%^&*()", "E-402 -- comment"):
        _search(engine, query)


def test_limit_is_capped(engine: Engine) -> None:
    assert len(_search(engine, PART, limit=10_000)) <= 50
