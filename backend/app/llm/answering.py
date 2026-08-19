"""Turning retrieved chunks into a cited answer.

Two things here are load-bearing and easy to get wrong.

**Structural separation.** The system prompt holds instructions; retrieved chunks
go in the user message, explicitly framed as untrusted data. A document that says
"ignore previous instructions" is then text inside a section the model has been
told is data — not a competing instruction at the same level as the real ones.

**Server-side citation verification.** The model is asked to cite [n], and every
[n] it emits is checked against the sources actually retrieved for THIS request.
A citation the model invented is removed. This matters because a citation is the
one part of an answer a user is likely to trust without checking, and a model
that hallucinates a source number produces an answer that looks verified and is
not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.knowledge.retrieval import SearchHit
from app.llm.base import LLMProvider, Message, Usage

PROMPTS = Path(__file__).parent.parent / "prompts"

REFUSAL = "I don't have enough information to answer that."

# [1], [12] — the citation form the prompt asks for.
_CITATION = re.compile(r"\[(\d+)\]")


@dataclass(frozen=True)
class Citation:
    """A verified reference from an answer back to a retrieved chunk."""

    index: int
    chunk_id: str
    document_id: str
    document_title: str


@dataclass(frozen=True)
class Answer:
    text: str
    citations: tuple[Citation, ...]
    refused: bool
    dropped_citations: tuple[int, ...]


@lru_cache(maxsize=8)
def load_prompt(name: str) -> str:
    """Prompts live in versioned files, not string literals.

    A prompt scattered through code cannot be diffed, reviewed, or rolled back,
    and every change to it is invisible in a commit that touches logic too.
    """
    return (PROMPTS / name).read_text(encoding="utf-8")


def build_messages(question: str, hits: list[SearchHit]) -> list[Message]:
    """Assemble the prompt: instructions in system, untrusted sources in user."""
    sources = "\n\n".join(
        f"[{index}] {hit.document_title}\n{hit.content}" for index, hit in enumerate(hits, start=1)
    )
    user = load_prompt("answer_v1.md").format(
        sources=sources or "(no sources were retrieved)",
        question=question,
    )
    return [
        Message(role="system", content=load_prompt("system_v1.md")),
        Message(role="user", content=user),
    ]


def verify_citations(
    text: str, hits: list[SearchHit]
) -> tuple[str, tuple[Citation, ...], tuple[int, ...]]:
    """Keep citations that resolve to a retrieved chunk; strip the rest.

    Returns the cleaned text, the verified citations, and the indices dropped —
    the last so the drop can be logged rather than silently swallowed. A model
    that invents citations is a signal worth seeing in traces.
    """
    valid_indices = set(range(1, len(hits) + 1))
    cited = [int(m) for m in _CITATION.findall(text)]

    dropped = tuple(sorted({index for index in cited if index not in valid_indices}))

    cleaned = text
    for index in dropped:
        # Remove the invented marker, and tidy the space it leaves behind.
        cleaned = cleaned.replace(f"[{index}]", "")
    cleaned = re.sub(r" {2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([.,;:])", r"\1", cleaned).strip()

    kept = sorted({index for index in cited if index in valid_indices})
    citations = tuple(
        Citation(
            index=index,
            chunk_id=str(hits[index - 1].chunk_id),
            document_id=str(hits[index - 1].document_id),
            document_title=hits[index - 1].document_title,
        )
        for index in kept
    )
    return cleaned, citations, dropped


def answer_question(
    question: str,
    hits: list[SearchHit],
    provider: LLMProvider,
    *,
    max_tokens: int = 1024,
) -> tuple[Answer, Usage | None]:
    """Generate a grounded answer, with the provider's usage when one was called."""
    if not hits:
        # No retrieval, no generation. Calling the model with no sources invites
        # it to answer from its own knowledge, which is exactly what grounding
        # is supposed to prevent — and it costs money to do so.
        return (
            Answer(text=REFUSAL, citations=(), refused=True, dropped_citations=()),
            None,
        )

    completion = provider.complete(build_messages(question, hits), max_tokens=max_tokens)
    cleaned, citations, dropped = verify_citations(completion.text, hits)
    refused = REFUSAL.lower() in cleaned.lower()

    return (
        Answer(
            text=cleaned,
            citations=citations,
            refused=refused,
            dropped_citations=dropped,
        ),
        completion.usage,
    )
