"""Ingestion: idempotency, and isolation between tenants.

The isolation test ingests BYTE-IDENTICAL text into two tenants. Identical content
is the point: it removes any chance that separation appears to work merely because
the text differed, and it exercises the content_hash uniqueness constraint, which
is scoped per tenant rather than globally.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.knowledge.embedding import FakeEmbeddings
from app.knowledge.ingest import ingest_directory


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

TENANT_A = uuid.UUID("cccc0000-0000-0000-0000-00000000000a")
TENANT_B = uuid.UUID("cccc0000-0000-0000-0000-00000000000b")

SHARED_TEXT = "# Refund Policy\n\nRefunds are processed within five business days.\n"


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean(engine: Engine) -> Iterator[None]:
    def _wipe() -> None:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM chunk WHERE tenant_id IN (:a, :b)"),
                {"a": TENANT_A, "b": TENANT_B},
            )
            conn.execute(
                text("DELETE FROM document WHERE tenant_id IN (:a, :b)"),
                {"a": TENANT_A, "b": TENANT_B},
            )
            conn.execute(
                text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": TENANT_A, "b": TENANT_B}
            )

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO tenant (id, slug, name) VALUES
                  (:a, 'ing-a', 'Ingest A'), (:b, 'ing-b', 'Ingest B')
            """),
            {"a": TENANT_A, "b": TENANT_B},
        )
    yield
    _wipe()


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    (tmp_path / "refunds.md").write_text(SHARED_TEXT, encoding="utf-8")
    (tmp_path / "shipping.md").write_text("# Shipping\n\nShipping is free.\n", encoding="utf-8")
    (tmp_path / "ignored.pdf").write_bytes(b"%PDF-1.4 not supported yet")
    return tmp_path


def _ingest(engine: Engine, corpus: Path, tenant_id: uuid.UUID, labels: list[str]) -> object:
    with Session(engine) as session, session.begin():
        return ingest_directory(
            session,
            directory=corpus,
            tenant_id=tenant_id,
            labels=labels,
            provider=FakeEmbeddings(),
        )


# A table name cannot be a bound parameter, so it is chosen from a fixed set
# rather than interpolated from an argument.
_COUNT_QUERIES = {
    "document": text("SELECT count(*) FROM document WHERE tenant_id = :t"),
    "chunk": text("SELECT count(*) FROM chunk WHERE tenant_id = :t"),
}


def _count(engine: Engine, table: str, tenant_id: uuid.UUID) -> int:
    with engine.connect() as conn:
        return conn.execute(_COUNT_QUERIES[table], {"t": tenant_id}).scalar() or 0


def test_ingest_creates_documents_and_chunks(engine: Engine, corpus: Path) -> None:
    result = _ingest(engine, corpus, TENANT_A, ["public"])

    assert result.documents_created == 2, "only .md and .txt should be ingested"
    assert result.chunks_created >= 2
    assert _count(engine, "document", TENANT_A) == 2


def test_ingest_is_idempotent(engine: Engine, corpus: Path) -> None:
    """Re-ingesting an unchanged corpus must not duplicate it.

    Duplicates are not merely wasteful: identical chunks crowd each other out of
    the top-k, so the same answer is returned five times instead of five answers.
    """
    first = _ingest(engine, corpus, TENANT_A, ["public"])
    chunks_after_first = _count(engine, "chunk", TENANT_A)

    second = _ingest(engine, corpus, TENANT_A, ["public"])

    assert second.documents_created == 0
    assert second.documents_skipped == first.documents_created
    assert _count(engine, "chunk", TENANT_A) == chunks_after_first


def test_identical_content_is_isolated_per_tenant(engine: Engine, corpus: Path) -> None:
    """Byte-identical text ingested for two tenants stays two separate corpora.

    The content_hash constraint is (tenant_id, content_hash), not content_hash
    alone — a global constraint would make tenant B's ingestion silently no-op
    because tenant A got there first.
    """
    _ingest(engine, corpus, TENANT_A, ["public"])
    result_b = _ingest(engine, corpus, TENANT_B, ["public"])

    assert result_b.documents_created == 2, "tenant B's identical content was skipped"
    assert _count(engine, "document", TENANT_A) == 2
    assert _count(engine, "document", TENANT_B) == 2

    with engine.connect() as conn:
        rows = (
            conn.execute(
                text("SELECT DISTINCT tenant_id FROM chunk WHERE tenant_id IN (:a, :b)"),
                {"a": TENANT_A, "b": TENANT_B},
            )
            .scalars()
            .all()
        )
    assert set(rows) == {TENANT_A, TENANT_B}


def test_every_chunk_carries_tenant_and_embedding_metadata(engine: Engine, corpus: Path) -> None:
    """Invariant #2, plus the model tag that keeps a mixed-model corpus detectable."""
    _ingest(engine, corpus, TENANT_A, ["public"])

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT tenant_id, embedding_model, embedding_dim
                FROM chunk WHERE tenant_id = :t
            """),
            {"t": TENANT_A},
        ).all()

    assert rows
    for row in rows:
        assert row.tenant_id == TENANT_A
        assert row.embedding_model == "fake/deterministic"
        assert row.embedding_dim == 1536


def test_labels_are_recorded_for_authorization(engine: Engine, corpus: Path) -> None:
    """Retrieval filters on these in stage 4; an empty set means visible to nobody."""
    _ingest(engine, corpus, TENANT_A, ["public", "finance"])

    with engine.connect() as conn:
        labels = (
            conn.execute(
                text("SELECT DISTINCT labels FROM document WHERE tenant_id = :t"), {"t": TENANT_A}
            )
            .scalars()
            .all()
        )

    assert all(set(row) == {"public", "finance"} for row in labels)
