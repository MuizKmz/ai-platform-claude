"""Chat endpoint: retrieve, generate, verify citations, respond.

Retrieval and generation are separate spans with separate latencies, so a slow
request can be attributed to the right half rather than guessed at.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import CurrentPrincipal, LLMLimited, TenantDB
from app.core.config import settings
from app.core.quota import record_spend
from app.core.security import Principal
from app.knowledge.embedding import get_embedding_provider
from app.knowledge.retrieval import SearchHit, search
from app.llm.answering import REFUSAL, answer_question, build_messages, verify_citations
from app.llm.base import LLMError, LLMProvider, LLMTimeoutError, Usage
from app.llm.providers.openai_provider import OpenAIProvider
from app.observability.tracing import new_trace_id, trace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["chat"])


def get_llm() -> LLMProvider:
    """The provider the endpoint uses. One place to swap; tests patch this."""
    return OpenAIProvider()


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: uuid.UUID | None = None
    stream: bool = False


class CitationOut(BaseModel):
    index: int
    chunk_id: str
    document_id: str
    document_title: str
    # The passage the claim came from. Returned inline rather than behind a
    # second request: a reader deciding whether to trust an answer should not
    # have to make another round trip to see the evidence for it.
    content: str


class ChatResponse(BaseModel):
    answer: str
    citations: list[CitationOut]
    refused: bool
    conversation_id: uuid.UUID
    trace_id: str
    usage: dict[str, float | int | str]


def _retrieve(db: Session, principal: Principal, question: str) -> list[SearchHit]:
    return search(
        db,
        query=question,
        tenant_id=principal.tenant_id,
        allowed_labels=principal.allowed_labels,
        provider=get_embedding_provider(),
        limit=settings.rag_top_k,
    )


def _ensure_conversation(
    db: Session, principal: Principal, conversation_id: uuid.UUID | None
) -> uuid.UUID:
    """Reuse a conversation or start one.

    A supplied id is looked up under RLS, so a caller cannot attach messages to
    another tenant's conversation by guessing its id — the row simply is not
    visible, and a fresh conversation is created instead.
    """
    if conversation_id is not None:
        found = db.execute(
            text("SELECT id FROM conversation WHERE id = :id"), {"id": conversation_id}
        ).scalar()
        if found:
            return uuid.UUID(str(found))

    new_id = uuid.uuid4()
    db.execute(
        text("INSERT INTO conversation (id, tenant_id, user_id) VALUES (:id, :t, :u)"),
        {"id": new_id, "t": principal.tenant_id, "u": principal.user_id},
    )
    return new_id


def _record_message(
    db: Session,
    *,
    conversation_id: uuid.UUID,
    principal: Principal,
    role: str,
    content: str,
    citations: list[dict[str, object]] | None = None,
    model: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cost_usd: float | None = None,
    refused: bool = False,
) -> None:
    db.execute(
        text("""
            INSERT INTO conversation_message
              (id, tenant_id, conversation_id, role, content, citations, model,
               prompt_tokens, completion_tokens, cost_usd, refused)
            VALUES
              (:id, :t, :c, :role, :content, CAST(:citations AS json), :model,
               :pt, :ct, :cost, :refused)
        """),
        {
            "id": uuid.uuid4(),
            "t": principal.tenant_id,
            "c": conversation_id,
            "role": role,
            "content": content,
            "citations": json.dumps(citations or []),
            "model": model,
            "pt": prompt_tokens,
            "ct": completion_tokens,
            "cost": cost_usd,
            "refused": refused,
        },
    )


@router.post("/chat", response_model=None)
def chat(
    request: ChatRequest,
    principal: CurrentPrincipal,
    db: TenantDB,
    # Rate limit and daily budget, both per tenant. Declared as a dependency so
    # it runs before the handler body — a 429 or 402 must cost nothing.
    _limits: LLMLimited = None,
) -> ChatResponse | StreamingResponse:
    """Answer a question from the tenant's permitted documents."""
    provider = get_llm()
    trace_id = new_trace_id()

    with trace(db, "knowledge.retrieve", tenant_id=principal.tenant_id, trace_id=trace_id) as span:
        hits = _retrieve(db, principal, request.question)
        span.set_attribute("result_count", len(hits))

    if request.stream:
        return _stream_response(request, hits, provider, trace_id, principal.tenant_id)

    with trace(db, "llm.generate", tenant_id=principal.tenant_id, trace_id=trace_id) as span:
        try:
            answer, usage = answer_question(
                request.question, hits, provider, max_tokens=settings.llm_max_tokens
            )
        except LLMTimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="The model did not respond in time.",
            ) from exc
        except LLMError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Generation failed.",
            ) from exc

        cost = provider.cost_of(usage) if usage else 0.0
        span.token_cost = usage.total_tokens if usage else 0
        span.set_attribute("cost_usd", cost)
        span.set_attribute("refused", answer.refused)
        span.set_attribute("citations_kept", len(answer.citations))
        span.set_attribute("citations_dropped", len(answer.dropped_citations))

    if answer.dropped_citations:
        # A model inventing source numbers is worth seeing, not swallowing.
        logger.warning(
            "dropped %d unverifiable citation(s) for tenant %s",
            len(answer.dropped_citations),
            principal.tenant_id,
        )

    conversation_id = _ensure_conversation(db, principal, request.conversation_id)
    _record_message(
        db,
        conversation_id=conversation_id,
        principal=principal,
        role="user",
        content=request.question,
    )
    citations_out = [
        CitationOut(
            index=c.index,
            chunk_id=c.chunk_id,
            document_id=c.document_id,
            document_title=c.document_title,
            content=c.content,
        )
        for c in answer.citations
    ]
    citations: list[dict[str, object]] = [dict(c) for c in citations_out]
    _record_message(
        db,
        conversation_id=conversation_id,
        principal=principal,
        role="assistant",
        content=answer.text,
        citations=citations,
        model=provider.model,
        prompt_tokens=usage.prompt_tokens if usage else None,
        completion_tokens=usage.completion_tokens if usage else None,
        cost_usd=cost,
        refused=answer.refused,
    )

    # After the money is spent, and outside the request transaction: the
    # provider was billed whether or not this request commits.
    record_spend(principal.tenant_id, cost)

    return ChatResponse(
        answer=answer.text,
        citations=citations_out,
        refused=answer.refused,
        conversation_id=conversation_id,
        trace_id=trace_id,
        usage={
            "prompt_tokens": usage.prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
            "cost_usd": round(cost, 6),
            "model": provider.model,
        },
    )


def _stream_response(
    request: ChatRequest,
    hits: list[SearchHit],
    provider: LLMProvider,
    trace_id: str,
    tenant_id: uuid.UUID,
) -> StreamingResponse:
    """Server-Sent Events.

    Citations are verified only once the full text is known, so they arrive as a
    final event rather than inline — a citation cannot be checked against sources
    until the sentence containing it has finished streaming.
    """

    def events() -> Iterator[str]:
        if not hits:
            yield _sse("token", {"text": REFUSAL})
            yield _sse("done", {"refused": True, "citations": [], "trace_id": trace_id})
            return

        collected: list[str] = []
        try:
            for piece in provider.stream(
                build_messages(request.question, hits), max_tokens=settings.llm_max_tokens
            ):
                collected.append(piece)
                yield _sse("token", {"text": piece})
        except LLMTimeoutError:
            yield _sse("error", {"detail": "The model did not respond in time."})
            return
        except LLMError:
            yield _sse("error", {"detail": "Generation failed."})
            return

        cleaned, citations, dropped = verify_citations("".join(collected), hits)
        if dropped:
            logger.warning("dropped %d unverifiable citation(s) in stream", len(dropped))

        # The streaming path spends money too, and until now spent it silently:
        # a tenant that always streamed would have consumed no quota at all.
        # Streaming yields no Usage object, so the tokens are counted from the
        # prompt and the collected text — an estimate, and a far better one than
        # zero.
        prompt_text = "".join(message.content for message in build_messages(request.question, hits))
        estimated = Usage(
            prompt_tokens=provider.count_tokens(prompt_text),
            completion_tokens=provider.count_tokens(cleaned),
        )
        record_spend(tenant_id, provider.cost_of(estimated))

        yield _sse(
            "done",
            {
                "refused": REFUSAL.lower() in cleaned.lower(),
                "citations": [
                    {
                        "index": c.index,
                        "chunk_id": c.chunk_id,
                        "document_id": c.document_id,
                        "document_title": c.document_title,
                    }
                    for c in citations
                ],
                "trace_id": trace_id,
            },
        )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
