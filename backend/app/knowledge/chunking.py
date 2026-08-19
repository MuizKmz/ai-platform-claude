"""Splitting documents into retrievable chunks.

Chunk boundaries decide retrieval quality more than any other single choice here.
A chunk that splits mid-sentence retrieves badly because the embedding describes
half a thought; a chunk spanning three unrelated sections retrieves badly because
the embedding is an average of three topics and close to none of them.

So the splitter is structure-aware: it prefers to break where the author already
broke — headings, then paragraphs, then sentences — and only splits mid-sentence
when a single sentence exceeds the budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Characters, not tokens. Tokenisation is model-specific and would couple chunking
# to the embedding provider; ~4 chars per token is close enough for sizing, and the
# provider validates the real limit.
DEFAULT_CHUNK_SIZE = 1200
DEFAULT_OVERLAP = 150

# A heading line, so a section can carry its own title into its embedding.
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Chunk:
    """One retrievable slice, plus the context needed to cite it."""

    ordinal: int
    content: str
    heading: str | None = None


def chunk_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Split text into overlapping, structure-aware chunks.

    Overlap exists so a fact sitting on a boundary is not lost: it appears at the
    end of one chunk and the start of the next, and either can retrieve it.
    """
    if overlap >= chunk_size:
        # Otherwise each chunk re-emits its predecessor's tail and the loop cannot
        # advance — a silent infinite loop rather than a visible error.
        raise ValueError("overlap must be smaller than chunk_size")

    text = text.strip()
    if not text:
        return []

    chunks: list[Chunk] = []
    for heading, section in _split_by_heading(text):
        for piece in _split_section(section, chunk_size, overlap):
            chunks.append(Chunk(ordinal=len(chunks), content=piece, heading=heading))
    return chunks


def _split_by_heading(text: str) -> list[tuple[str | None, str]]:
    """Break into (heading, body) sections at Markdown headings.

    Plain text with no headings comes back as one unnamed section, so .txt files
    flow through the same path.
    """
    matches = list(_HEADING.finditer(text))
    if not matches:
        return [(None, text)]

    sections: list[tuple[str | None, str]] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append((None, preamble))

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if body:
            sections.append((match.group(2).strip(), body))
    return sections


def _split_section(section: str, chunk_size: int, overlap: int) -> list[str]:
    """Pack a section into chunks, breaking at the largest natural unit that fits."""
    if len(section) <= chunk_size:
        return [section]

    units = _PARAGRAPH_BREAK.split(section)
    # A single oversized paragraph is re-split at sentence boundaries, and an
    # oversized sentence is finally cut by length — the last resort, not the default.
    expanded: list[str] = []
    for unit in units:
        if len(unit) <= chunk_size:
            expanded.append(unit)
        else:
            expanded.extend(_split_oversized(unit, chunk_size))

    chunks: list[str] = []
    current = ""
    for unit in expanded:
        candidate = f"{current}\n\n{unit}" if current else unit
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = _tail(chunks[-1], overlap) + "\n\n" + unit if chunks and overlap else unit

    if current:
        chunks.append(current)
    return [c.strip() for c in chunks if c.strip()]


def _split_oversized(unit: str, chunk_size: int) -> list[str]:
    """Split a too-long paragraph at sentence ends, then by length if needed."""
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(unit):
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        if current:
            pieces.append(current)
        # A single sentence longer than the budget: hard-cut it.
        while len(sentence) > chunk_size:
            pieces.append(sentence[:chunk_size])
            sentence = sentence[chunk_size:]
        current = sentence
    if current:
        pieces.append(current)
    return pieces


def _tail(text: str, overlap: int) -> str:
    """The trailing overlap window, trimmed to a word boundary."""
    if overlap <= 0 or len(text) <= overlap:
        return text
    window = text[-overlap:]
    space = window.find(" ")
    return window[space + 1 :] if space != -1 else window
