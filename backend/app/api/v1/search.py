"""Search endpoint.

Phase 1 returns chunks, not answers. There is no LLM in this path by design:
retrieval quality is the thing being built, and generation would hide its defects
behind fluent prose.
"""

import uuid

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import CurrentPrincipal, TenantDB
from app.core.config import settings
from app.knowledge.embedding import get_embedding_provider
from app.knowledge.hybrid import hybrid_search
from app.knowledge.retrieval import MAX_LIMIT, SearchHit, search
from app.observability.tracing import new_trace_id, trace

router = APIRouter(prefix="/v1", tags=["knowledge"])


class SearchResult(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    content: str
    ordinal: int
    score: float


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    # Echoed so a caller can see what they were scoped to. This is disclosure of
    # the caller's OWN authority, which they already hold in their token — never
    # of what was filtered away, which would leak the existence of other data.
    filtered_by: dict[str, list[str] | str]
    trace_id: str


def _retrieve(db, q: str, principal, limit: int) -> list[SearchHit]:  # type: ignore[no-untyped-def]
    """Vector search, or hybrid when enabled.

    A flag rather than a code path choice, so switching strategy is a config edit
    and the eval harness can compare the two without a deploy.
    """
    provider = get_embedding_provider()
    if settings.hybrid_search_enabled:
        return [
            fused.hit
            for fused in hybrid_search(
                db,
                query=q,
                tenant_id=principal.tenant_id,
                allowed_labels=principal.allowed_labels,
                provider=provider,
                limit=limit,
            )
        ]
    return search(
        db,
        query=q,
        tenant_id=principal.tenant_id,
        allowed_labels=principal.allowed_labels,
        provider=provider,
        limit=limit,
    )


@router.get("/search", response_model=SearchResponse)
def search_endpoint(
    principal: CurrentPrincipal,
    db: TenantDB,
    q: str = Query(min_length=1, max_length=1000, description="Search query"),
    limit: int = Query(default=5, ge=1, le=MAX_LIMIT),
) -> SearchResponse:
    """Retrieve chunks the caller is permitted to see, ranked by similarity.

    Note what is absent from the signature: there is no tenant parameter. Tenancy
    comes from the Principal, and the Principal comes from a verified token.
    """
    trace_id = new_trace_id()

    with trace(db, "knowledge.search", tenant_id=principal.tenant_id, trace_id=trace_id) as span:
        hits = _retrieve(db, q, principal, limit)
        # Counts and lengths only. Query text and chunk content stay out of the
        # trace: a span table is a second copy of tenant data that nobody thinks
        # to review.
        span.set_attribute("result_count", len(hits))
        span.set_attribute("query_length", len(q))
        span.set_attribute("label_count", len(principal.allowed_labels))

    return SearchResponse(
        query=q,
        results=[
            SearchResult(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                document_title=hit.document_title,
                content=hit.content,
                ordinal=hit.ordinal,
                # Rounded at the boundary. Float64 precision on a similarity
                # score is noise, not information, and it makes responses hard
                # to read and hard to diff in tests.
                score=round(hit.score, 4),
            )
            for hit in hits
        ],
        filtered_by={
            "tenant_id": str(principal.tenant_id),
            "labels": list(principal.allowed_labels),
        },
        trace_id=trace_id,
    )
