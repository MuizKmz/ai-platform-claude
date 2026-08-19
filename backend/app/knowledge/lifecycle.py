"""Document lifecycle: supersede, delete, reindex.

Three operations that a corpus needs the moment it stops being a demo, and each
has a failure mode worth stating.

**Delete must be immediate and complete.** A document removed from the UI but
still answering questions is a data-retention breach with a paper trail saying it
was deleted. Chunks go with it, in the same transaction.

**Supersede must not delete.** A citation issued last week should still resolve to
the text that was actually cited, so old revisions are tombstoned rather than
removed — and excluded from retrieval, so nobody is answered from a stale one.

**Reindex must preserve labels.** Re-embedding touches vectors, never permissions.
A reindex that resets labels to a default silently widens who can see a document,
and nothing about the operation's name suggests it could.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.knowledge.embedding import EmbeddingProvider


@dataclass
class DeleteResult:
    documents_deleted: int = 0
    chunks_deleted: int = 0


@dataclass
class ReindexResult:
    documents_reindexed: int = 0
    chunks_reembedded: int = 0


def delete_document(
    session: Session, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> DeleteResult:
    """Hard-delete a document and every chunk of it.

    Scoped by tenant_id as well as document_id. The id alone would be enough under
    RLS, but a query that reads correctly on its own is worth more than one that
    depends on a policy the reader has to remember.
    """
    chunks = session.execute(
        text("DELETE FROM chunk WHERE tenant_id = :t AND document_id = :d RETURNING id"),
        {"t": tenant_id, "d": document_id},
    ).all()
    documents = session.execute(
        text("DELETE FROM document WHERE tenant_id = :t AND id = :d RETURNING id"),
        {"t": tenant_id, "d": document_id},
    ).all()
    return DeleteResult(documents_deleted=len(documents), chunks_deleted=len(chunks))


def supersede_document(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    old_document_id: uuid.UUID,
    new_document_id: uuid.UUID,
) -> None:
    """Tombstone a document, pointing at the revision that replaced it.

    The old row and its chunks stay in place. Retrieval excludes them; an audit
    trail or an already-issued citation can still resolve them.
    """
    session.execute(
        text("""
            UPDATE document
            SET superseded_at = :now, superseded_by = :new
            WHERE tenant_id = :t AND id = :old AND superseded_at IS NULL
        """),
        {
            "now": datetime.now(UTC),
            "new": new_document_id,
            "t": tenant_id,
            "old": old_document_id,
        },
    )


def find_live_by_source(
    session: Session, *, tenant_id: uuid.UUID, source_path: str
) -> tuple[uuid.UUID, int] | None:
    """The current revision at a source path, as (id, version), or None.

    Identity across revisions is source_path within a tenant: the same file,
    edited. Content hash identifies a *revision*, not a document.
    """
    row = session.execute(
        text("""
            SELECT id, version FROM document
            WHERE tenant_id = :t AND source_path = :p AND superseded_at IS NULL
            ORDER BY version DESC LIMIT 1
        """),
        {"t": tenant_id, "p": source_path},
    ).one_or_none()
    return (row.id, row.version) if row else None


def reindex_document(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    provider: EmbeddingProvider,
) -> ReindexResult:
    """Re-embed a document's chunks with the current provider.

    Needed when the embedding model changes: a corpus embedded with two models is
    unsearchable, because cosine distance between vectors from different models is
    a meaningless number rather than an error.

    Chunk text, ordinals, and the document's labels are untouched. Only the
    vectors and the model tag change.
    """
    rows = session.execute(
        text("""
            SELECT id, content FROM chunk
            WHERE tenant_id = :t AND document_id = :d
            ORDER BY ordinal
        """),
        {"t": tenant_id, "d": document_id},
    ).all()
    if not rows:
        return ReindexResult()

    vectors = provider.embed([row.content for row in rows])
    for row, vector in zip(rows, vectors, strict=True):
        session.execute(
            text("""
                UPDATE chunk
                SET embedding = :emb, embedding_model = :model, embedding_dim = :dim
                WHERE tenant_id = :t AND id = :id
            """),
            {
                "emb": str(vector),
                "model": provider.model,
                "dim": provider.dimension,
                "t": tenant_id,
                "id": row.id,
            },
        )
    return ReindexResult(documents_reindexed=1, chunks_reembedded=len(rows))
