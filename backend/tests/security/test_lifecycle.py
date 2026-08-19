"""Document lifecycle: delete, supersede, reindex.

These are security tests, not merely functional ones. A document that survives
deletion is a retention breach; a stale revision that keeps answering is a wrong
answer with a citation attached; a reindex that resets labels silently widens who
can see a document.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.knowledge.embedding import FakeEmbeddings
from app.knowledge.ingest import ingest_directory
from app.knowledge.lifecycle import delete_document, reindex_document, supersede_document
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

TENANT = uuid.UUID("1a1a0000-0000-0000-0000-00000000001a")

V1_TEXT = "# Refunds\n\nRefunds are processed within five business days.\n"
V2_TEXT = "# Refunds\n\nRefunds are processed within ten business days.\n"


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean(engine: Engine) -> Iterator[None]:
    def _wipe() -> None:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM chunk WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM document WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": TENANT})

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenant (id, slug, name) VALUES (:t, 'life-test', 'Lifecycle')"),
            {"t": TENANT},
        )
    yield
    _wipe()


def _ingest(engine: Engine, directory: Path, labels: list[str] | None = None) -> object:
    with Session(engine) as session, session.begin():
        return ingest_directory(
            session,
            directory=directory,
            tenant_id=TENANT,
            labels=labels or ["public"],
            provider=FakeEmbeddings(),
        )


def _search(engine: Engine, query: str, labels: tuple[str, ...] = ("public",)) -> list[object]:
    with Session(engine) as session:
        return list(
            search(
                session,
                query=query,
                tenant_id=TENANT,
                allowed_labels=labels,
                provider=FakeEmbeddings(),
                limit=10,
            )
        )


def _document_ids(engine: Engine) -> list[uuid.UUID]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT id FROM document WHERE tenant_id = :t ORDER BY version"),
                {"t": TENANT},
            ).scalars()
        )


def _count(engine: Engine, table: str) -> int:
    queries = {
        "document": text("SELECT count(*) FROM document WHERE tenant_id = :t"),
        "chunk": text("SELECT count(*) FROM chunk WHERE tenant_id = :t"),
    }
    with engine.connect() as conn:
        return conn.execute(queries[table], {"t": TENANT}).scalar() or 0


# --- delete -------------------------------------------------------------------


def test_document_delete_cascades_to_chunks(engine: Engine, tmp_path: Path) -> None:
    """Chunks go with the document. An orphaned chunk is content with no title,
    no labels, and no way to reason about who may see it."""
    (tmp_path / "refunds.md").write_text(V1_TEXT, encoding="utf-8")
    _ingest(engine, tmp_path)
    assert _count(engine, "chunk") > 0

    document_id = _document_ids(engine)[0]
    with Session(engine) as session, session.begin():
        result = delete_document(session, tenant_id=TENANT, document_id=document_id)

    assert result.documents_deleted == 1
    assert result.chunks_deleted > 0
    assert _count(engine, "document") == 0
    assert _count(engine, "chunk") == 0


def test_deleted_document_is_immediately_unretrievable(engine: Engine, tmp_path: Path) -> None:
    """Verified by search, not by inspecting a table.

    "Deleted but still answering" is a retention breach with a paper trail saying
    it was deleted.
    """
    (tmp_path / "refunds.md").write_text(V1_TEXT, encoding="utf-8")
    _ingest(engine, tmp_path)
    assert _search(engine, "Refunds are processed within five business days.")

    with Session(engine) as session, session.begin():
        delete_document(session, tenant_id=TENANT, document_id=_document_ids(engine)[0])

    assert _search(engine, "Refunds are processed within five business days.") == []


def test_delete_is_scoped_to_the_tenant(engine: Engine, tmp_path: Path) -> None:
    """A document id from another tenant must not delete anything."""
    (tmp_path / "refunds.md").write_text(V1_TEXT, encoding="utf-8")
    _ingest(engine, tmp_path)
    document_id = _document_ids(engine)[0]

    with Session(engine) as session, session.begin():
        result = delete_document(session, tenant_id=uuid.uuid4(), document_id=document_id)

    assert result.documents_deleted == 0
    assert _count(engine, "document") == 1


# --- supersede ----------------------------------------------------------------


def test_superseded_revision_is_not_retrieved(engine: Engine, tmp_path: Path) -> None:
    """The roadmap's test: v1 stops being retrieved once v2 replaces it.

    Answering from a stale revision is as wrong as answering from an unauthorized
    one, and harder to notice because the citation looks legitimate.
    """
    source = tmp_path / "refunds.md"
    source.write_text(V1_TEXT, encoding="utf-8")
    _ingest(engine, tmp_path)

    source.write_text(V2_TEXT, encoding="utf-8")
    result = _ingest(engine, tmp_path)

    assert result.documents_created == 1
    assert result.documents_superseded == 1

    contents = [hit.content for hit in _search(engine, "How long do refunds take?")]
    assert any("ten business days" in c for c in contents), "v2 should be retrievable"
    assert not any("five business days" in c for c in contents), "v1 is still retrievable"


def test_superseded_row_is_retained_not_deleted(engine: Engine, tmp_path: Path) -> None:
    """A citation issued last week must still resolve to the text that was cited."""
    source = tmp_path / "refunds.md"
    source.write_text(V1_TEXT, encoding="utf-8")
    _ingest(engine, tmp_path)
    source.write_text(V2_TEXT, encoding="utf-8")
    _ingest(engine, tmp_path)

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT version, superseded_at, superseded_by
                FROM document WHERE tenant_id = :t ORDER BY version
            """),
            {"t": TENANT},
        ).all()

    assert len(rows) == 2, "the old revision was deleted rather than tombstoned"
    assert rows[0].version == 1
    assert rows[0].superseded_at is not None
    assert rows[0].superseded_by is not None
    assert rows[1].version == 2
    assert rows[1].superseded_at is None


def test_reingesting_unchanged_content_does_not_create_a_version(
    engine: Engine, tmp_path: Path
) -> None:
    """Idempotency must survive versioning: an unchanged file is still a no-op."""
    (tmp_path / "refunds.md").write_text(V1_TEXT, encoding="utf-8")
    _ingest(engine, tmp_path)

    second = _ingest(engine, tmp_path)

    assert second.documents_created == 0
    assert second.documents_superseded == 0
    assert _count(engine, "document") == 1


def test_supersede_is_idempotent(engine: Engine, tmp_path: Path) -> None:
    """Tombstoning an already-tombstoned row must not overwrite the first pointer."""
    source = tmp_path / "refunds.md"
    source.write_text(V1_TEXT, encoding="utf-8")
    _ingest(engine, tmp_path)
    source.write_text(V2_TEXT, encoding="utf-8")
    _ingest(engine, tmp_path)

    old_id, new_id = _document_ids(engine)
    with engine.connect() as conn:
        before = conn.execute(
            text("SELECT superseded_by FROM document WHERE id = :id"), {"id": old_id}
        ).scalar()

    with Session(engine) as session, session.begin():
        supersede_document(
            session, tenant_id=TENANT, old_document_id=old_id, new_document_id=uuid.uuid4()
        )

    with engine.connect() as conn:
        after = conn.execute(
            text("SELECT superseded_by FROM document WHERE id = :id"), {"id": old_id}
        ).scalar()

    assert after == before == new_id


# --- reindex ------------------------------------------------------------------


def test_reindex_preserves_permissions(engine: Engine, tmp_path: Path) -> None:
    """The roadmap's test, and the one with the quietest failure mode.

    Re-embedding touches vectors. A reindex that also reset labels would widen
    who can see a document, and nothing in the operation's name suggests it could.
    """
    (tmp_path / "salaries.md").write_text("# Pay\n\nThe CEO earns 450000.\n", encoding="utf-8")
    _ingest(engine, tmp_path, labels=["finance", "restricted"])
    document_id = _document_ids(engine)[0]

    with Session(engine) as session, session.begin():
        result = reindex_document(
            session,
            tenant_id=TENANT,
            document_id=document_id,
            provider=FakeEmbeddings(model="fake/v2"),
        )

    assert result.chunks_reembedded > 0
    with engine.connect() as conn:
        labels = conn.execute(
            text("SELECT labels FROM document WHERE id = :id"), {"id": document_id}
        ).scalar()
        models = (
            conn.execute(
                text("SELECT DISTINCT embedding_model FROM chunk WHERE document_id = :id"),
                {"id": document_id},
            )
            .scalars()
            .all()
        )

    assert sorted(labels) == ["finance", "restricted"], "labels changed during reindex"
    assert models == ["fake/v2"], "the model tag was not updated"

    # And the document is still invisible to someone without the labels.
    assert _search(engine, "The CEO earns 450000.", labels=("public",)) == []


def test_reindex_preserves_chunk_text_and_order(engine: Engine, tmp_path: Path) -> None:
    """Only vectors change. Rewriting content during a reindex would invalidate
    every citation already issued against those chunks."""
    (tmp_path / "refunds.md").write_text(V1_TEXT, encoding="utf-8")
    _ingest(engine, tmp_path)
    document_id = _document_ids(engine)[0]

    def snapshot() -> list[tuple[int, str]]:
        with engine.connect() as conn:
            return [
                (row.ordinal, row.content)
                for row in conn.execute(
                    text(
                        "SELECT ordinal, content FROM chunk "
                        "WHERE document_id = :id ORDER BY ordinal"
                    ),
                    {"id": document_id},
                ).all()
            ]

    before = snapshot()
    with Session(engine) as session, session.begin():
        reindex_document(
            session,
            tenant_id=TENANT,
            document_id=document_id,
            provider=FakeEmbeddings(model="fake/v2"),
        )

    assert snapshot() == before
