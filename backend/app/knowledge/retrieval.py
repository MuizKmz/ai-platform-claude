"""Vector search with authorization applied before ranking.

Security invariant #3: the filter is in the WHERE clause, not applied to results.
Two reasons this is not a style preference.

1. Post-filtering breaks top-k. Ask for 5, discard 3 the caller may not see,
   return 2 — the user silently receives a worse answer than they asked for, and
   nothing in the response says so.
2. Post-filtering means the rows already left the database. They were in process
   memory, and potentially in a log or a trace. "Retrieved then hidden" is not
   "not retrieved".

RLS backstops this: even if this query forgot its tenant predicate, the policies
from migration 1fb64d2aaf50 would still deny the rows. Belt and braces, because
the application filter is the fast path and the database guarantee is the floor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.knowledge.embedding import EmbeddingProvider

MAX_LIMIT = 50


@dataclass(frozen=True)
class SearchHit:
    """One retrieved chunk, with everything needed to cite it."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    content: str
    ordinal: int
    score: float


def search(
    session: Session,
    *,
    query: str,
    tenant_id: uuid.UUID,
    allowed_labels: tuple[str, ...],
    provider: EmbeddingProvider,
    limit: int = 5,
) -> list[SearchHit]:
    """Rank chunks by cosine similarity, restricted to what the caller may see.

    `allowed_labels` comes from the verified Principal. An empty set matches
    nothing — default-deny, not default-allow, so a user with no labels
    configured sees no documents rather than all of them.
    """
    if not query.strip():
        return []

    limit = max(1, min(limit, MAX_LIMIT))

    if not allowed_labels:
        # An empty array would make the overlap operator match nothing anyway;
        # returning early makes the intent explicit rather than incidental.
        return []

    (embedding,) = provider.embed([query])

    # && is array overlap: true when the document carries at least one label the
    # caller holds. A document with no labels overlaps nothing and is therefore
    # invisible to everyone, which is the default-deny the roadmap requires.
    #
    # superseded_at IS NULL excludes tombstoned revisions. It sits in the same
    # WHERE clause as the authorization predicates for the same reason: filtering
    # after the fact would mean a stale revision was retrieved, and a stale answer
    # is as wrong as an unauthorized one.
    #
    # <=> is pgvector's cosine distance, and the HNSW index is built for it.
    # Distance is 0 (identical) to 2 (opposite), so score = 1 - distance gives the
    # more familiar 1 (identical) to -1 ordering.
    rows = session.execute(
        text("""
            SELECT
                c.id            AS chunk_id,
                c.document_id   AS document_id,
                d.title         AS document_title,
                c.content       AS content,
                c.ordinal       AS ordinal,
                1 - (c.embedding <=> CAST(:embedding AS vector)) AS score
            FROM chunk c
            JOIN document d ON d.id = c.document_id
            WHERE c.tenant_id = :tenant_id
              AND d.tenant_id = :tenant_id
              AND d.superseded_at IS NULL
              AND d.labels && CAST(:labels AS varchar[])
            ORDER BY c.embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
        """),
        {
            "embedding": str(embedding),
            "tenant_id": tenant_id,
            "labels": list(allowed_labels),
            "limit": limit,
        },
    ).all()

    return [
        SearchHit(
            chunk_id=row.chunk_id,
            document_id=row.document_id,
            document_title=row.document_title,
            content=row.content,
            ordinal=row.ordinal,
            score=float(row.score),
        )
        for row in rows
    ]
