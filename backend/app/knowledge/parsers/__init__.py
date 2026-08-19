"""Document parsers, selected by file extension."""

from __future__ import annotations

from pathlib import Path

from app.knowledge.parsers.base import (
    Block,
    BlockType,
    DocumentParser,
    ParsedDocument,
    ParseError,
)
from app.knowledge.parsers.docx import DocxParser
from app.knowledge.parsers.html import HtmlParser
from app.knowledge.parsers.markdown import MarkdownParser
from app.knowledge.parsers.pdf import PdfParser

_PARSERS: tuple[DocumentParser, ...] = (
    MarkdownParser(),
    PdfParser(),
    DocxParser(),
    HtmlParser(),
)

SUPPORTED_SUFFIXES = frozenset(suffix for parser in _PARSERS for suffix in parser.suffixes)


def parser_for(path: Path) -> DocumentParser | None:
    """The parser handling this file, or None if the format is unsupported."""
    suffix = path.suffix.lower()
    return next((p for p in _PARSERS if suffix in p.suffixes), None)


def parse_document(path: Path) -> ParsedDocument:
    """Parse a file, or raise ParseError if nothing handles it."""
    parser = parser_for(path)
    if parser is None:
        raise ParseError(f"unsupported file type: {path.suffix or '(none)'}")
    return parser.parse(path)


__all__ = [
    "SUPPORTED_SUFFIXES",
    "Block",
    "BlockType",
    "DocumentParser",
    "DocxParser",
    "HtmlParser",
    "MarkdownParser",
    "ParseError",
    "ParsedDocument",
    "PdfParser",
    "parse_document",
    "parser_for",
]
