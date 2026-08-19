"""The document parsing contract.

Parsers return typed **blocks**, not a string. That distinction is the whole point
of this interface: a table returned as plain text is indistinguishable from a
paragraph, and the chunker will happily break it at a row boundary. Half a table
embeds as nonsense and retrieves as nonsense.

So a parser's job is to say what each piece of a document *is*, and the chunker's
job is to respect that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable


class ParseError(Exception):
    """The document could not be read. Message is safe to log, not to return —
    it may contain a file path."""


class BlockType(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    LIST = "list"


@dataclass(frozen=True)
class Block:
    """One structural piece of a document."""

    type: BlockType
    text: str
    # Heading depth (1-6), for headings only.
    level: int | None = None
    # 1-based page number where known. PDFs have pages; DOCX and HTML do not.
    page: int | None = None
    # The nearest preceding heading, so a chunk can carry its section title into
    # the embedding even when the heading is a separate block.
    heading: str | None = None

    @property
    def is_atomic(self) -> bool:
        """True if this block must not be split.

        A table split across chunks loses its header row, and every subsequent
        row becomes a sequence of unlabelled values.
        """
        return self.type is BlockType.TABLE


@dataclass
class ParsedDocument:
    """A document reduced to ordered blocks, plus what we learned about it."""

    blocks: list[Block] = field(default_factory=list)
    page_count: int | None = None
    title: str | None = None

    @property
    def text(self) -> str:
        """Flattened text, for hashing and for callers that want the whole thing."""
        return "\n\n".join(block.text for block in self.blocks)


@runtime_checkable
class DocumentParser(Protocol):
    """What every format implementation provides."""

    @property
    def suffixes(self) -> tuple[str, ...]:
        """Lowercase file extensions this parser handles, including the dot."""
        ...

    def parse(self, path: Path) -> ParsedDocument:
        """Read a file into ordered blocks, or raise ParseError."""
        ...
