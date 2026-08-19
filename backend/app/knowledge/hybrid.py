"""Hybrid retrieval: dense vectors fused with keyword search.

Dense embeddings and keyword search fail in opposite directions, which is exactly
why fusing them beats either alone.

**Embeddings understand meaning and not much else.** "How long until I get my
money back?" retrieves a passage about refunds that shares no words with the
question — that is the whole value. But the embedding of `QN-1183-A` sits close
to the embedding of `QN-2013-A`, because they *look* alike; the model has no idea
they identify different parts. Enterprise text is full of these: part numbers,
error codes, SKUs, ticket ids.

**Keyword search is the mirror image.** It matches `QN-1183-A` exactly and cannot
connect "money back" to "refund" at all.

Fusion is by Reciprocal Rank Fusion, which combines *ranks* rather than scores.
That matters: a cosine similarity of 0.42 and a ts_rank of 0.089 are not
comparable quantities, and normalising them into one number requires a weighting
that is invented rather than measured. RRF only asks "where did each side put
this?", which needs no such invention.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.orm import Session

from app.knowledge.embedding import EmbeddingProvider
from app.knowledge.retrieval import MAX_LIMIT, SearchHit

# The RRF constant. 60 is the value from the original paper and the usual default.
# It damps the influence of top ranks: without it, a rank-1 hit on one side would
# dominate any amount of agreement further down the other.
RRF_K = 60

# How deep each retriever goes before fusion. Wider than the final limit, because
# a chunk ranked 8th by vectors and 9th by keywords can fuse into the top 5 — and
# would be invisible if each side only returned 5.
CANDIDATE_DEPTH = 30

# Weight applied to the keyword side of the fusion.
#
# Measured, not chosen. With equal weights the keyword retriever DEGRADED
# semantic Recall@1 from 0.88 to 0.59: OR-matching returns anything sharing a
# common word, and enough of that noise ranked highly to displace the right
# answer. See docs/experiments/hybrid-search-comparison.md.
#
# Keyword search earns its place on exact identifiers, where dense embeddings
# cannot tell QN-1183-A from QN-2013-A. It should break ties and rescue exact
# matches, not outvote a retriever that understands the question.
KEYWORD_WEIGHT = 0.3


@dataclass(frozen=True)
class FusedHit:
    """A search hit plus where each retriever placed it.

    The ranks are kept for the eval harness and for debugging a bad result: "the
    keyword side never saw this" and "both sides ranked it 20th" are different
    problems with different fixes.
    """

    hit: SearchHit
    score: float
    vector_rank: int | None
    keyword_rank: int | None


def hybrid_search(
    session: Session,
    *,
    query: str,
    tenant_id: uuid.UUID,
    allowed_labels: tuple[str, ...],
    provider: EmbeddingProvider,
    limit: int = 5,
    candidate_depth: int = CANDIDATE_DEPTH,
) -> list[FusedHit]:
    """Retrieve by vector and keyword, then fuse by reciprocal rank.

    Both sides apply the same tenant, label, and supersession predicates. That is
    not duplication for its own sake: a retriever that skipped them would be a
    second way into the data that the first one's WHERE clause was protecting.
    """
    if not query.strip() or not allowed_labels:
        return []

    limit = max(1, min(limit, MAX_LIMIT))
    (embedding,) = provider.embed([query])

    vector_rows = session.execute(
        text("""
            SELECT c.id, c.document_id, d.title, c.content, c.ordinal,
                   1 - (c.embedding <=> CAST(:embedding AS vector)) AS score
            FROM chunk c
            JOIN document d ON d.id = c.document_id
            WHERE c.tenant_id = :tenant_id
              AND d.tenant_id = :tenant_id
              AND d.superseded_at IS NULL
              AND d.labels && CAST(:labels AS varchar[])
            ORDER BY c.embedding <=> CAST(:embedding AS vector)
            LIMIT :depth
        """),
        {
            "embedding": str(embedding),
            "tenant_id": tenant_id,
            "labels": list(allowed_labels),
            "depth": candidate_depth,
        },
    ).all()

    # OR semantics, not AND. websearch_to_tsquery joins every word with AND, so
    # "What does error E-402 mean?" would require the chunk to contain "what",
    # "does", "error", "E-402" AND "mean" — and the error-codes table contains
    # none of the question words. A retriever must RANK by overlap, not demand
    # every term; requiring all of them is a filter, and a very strict one.
    #
    # Both configs are queried, matching how the column is built: 'english'
    # stems prose, 'simple' keeps identifiers like E-402 literal. The English
    # parser splits E-402 into an adjacency pair that never matches what it
    # indexed, which is why the simple vector exists at all.
    #
    # to_tsquery over a lexeme array cannot raise on user input the way parsing a
    # raw string can — the lexemes have already been produced by to_tsvector.
    keyword_rows = session.execute(
        text("""
            WITH q AS (
                SELECT
                    (
                        SELECT string_agg(lex, ' | ')
                        FROM unnest(tsvector_to_array(to_tsvector('english', :query))) AS lex
                    ) AS english_q,
                    (
                        SELECT string_agg(lex, ' | ')
                        FROM unnest(tsvector_to_array(to_tsvector('simple', :query))) AS lex
                    ) AS simple_q
            )
            SELECT c.id, c.document_id, d.title, c.content, c.ordinal,
                   greatest(
                       ts_rank(c.search_vector, q.english_q::tsquery),
                       ts_rank(c.search_vector, q.simple_q::tsquery)
                   ) AS score
            FROM chunk c
            JOIN document d ON d.id = c.document_id
            CROSS JOIN q
            WHERE c.tenant_id = :tenant_id
              AND d.tenant_id = :tenant_id
              AND d.superseded_at IS NULL
              AND d.labels && CAST(:labels AS varchar[])
              AND q.english_q IS NOT NULL
              AND (
                  c.search_vector @@ q.english_q::tsquery
                  OR c.search_vector @@ q.simple_q::tsquery
              )
            ORDER BY score DESC
            LIMIT :depth
        """),
        {
            "query": query,
            "tenant_id": tenant_id,
            "labels": list(allowed_labels),
            "depth": candidate_depth,
        },
    ).all()

    return _fuse(vector_rows, keyword_rows, limit=limit)


def _fuse(
    vector_rows: Sequence[Row[Any]], keyword_rows: Sequence[Row[Any]], *, limit: int
) -> list[FusedHit]:
    """Reciprocal Rank Fusion: score = sum over retrievers of 1 / (k + rank)."""
    vector_ranks = {row.id: index for index, row in enumerate(vector_rows, start=1)}
    keyword_ranks = {row.id: index for index, row in enumerate(keyword_rows, start=1)}

    rows_by_id = {row.id: row for row in vector_rows}
    rows_by_id.update({row.id: row for row in keyword_rows})

    fused: list[FusedHit] = []
    for chunk_id, row in rows_by_id.items():
        v_rank = vector_ranks.get(chunk_id)
        k_rank = keyword_ranks.get(chunk_id)
        score = 0.0
        if v_rank is not None:
            score += 1.0 / (RRF_K + v_rank)
        if k_rank is not None:
            score += KEYWORD_WEIGHT / (RRF_K + k_rank)

        fused.append(
            FusedHit(
                hit=SearchHit(
                    chunk_id=row.id,
                    document_id=row.document_id,
                    document_title=row.title,
                    content=row.content,
                    ordinal=row.ordinal,
                    score=score,
                ),
                score=score,
                vector_rank=v_rank,
                keyword_rank=k_rank,
            )
        )

    # Ties broken by chunk id so results are stable across runs — an eval that
    # reorders equal-scoring hits between runs reports noise as movement.
    fused.sort(key=lambda f: (-f.score, str(f.hit.chunk_id)))
    return fused[:limit]
