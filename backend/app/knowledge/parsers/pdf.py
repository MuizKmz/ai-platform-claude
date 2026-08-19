"""PDF parsing.

PDF has no concept of a paragraph, a heading, or a table. It positions glyphs on a
page. Everything structural here is therefore a heuristic reconstruction, and the
heuristics will be wrong on some real documents — which is exactly why parsers sit
behind an interface (see ADR 0002).

Two problems get handled explicitly because they wreck retrieval quietly:

**Repeated headers and footers.** A page header appearing on all 200 pages becomes
200 near-identical chunks that crowd out real content in the top-k. Lines that
recur on most pages are dropped.

**Tables.** Detected by run-of-lines sharing multiple column gaps, and emitted as a
single atomic block so the chunker cannot split them.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.knowledge.parsers.base import Block, BlockType, ParsedDocument, ParseError

# A line looks tabular if it has two or more runs of 2+ spaces, i.e. column gaps.
_COLUMN_GAP = re.compile(r"\S {2,}\S")
_HEADING_LIKE = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?[A-Z][^.!?]{2,80}$")

# A line on this fraction of pages or more is chrome, not content.
_REPEAT_THRESHOLD = 0.6
# Tables need at least this many consecutive tabular lines, so a single
# wide-spaced sentence is not mistaken for one.
_MIN_TABLE_ROWS = 2
# Above this, the first heading describes page 1 rather than the document.
_SHORT_DOCUMENT_PAGES = 3


class PdfParser:
    @property
    def suffixes(self) -> tuple[str, ...]:
        return (".pdf",)

    def parse(self, path: Path) -> ParsedDocument:
        try:
            reader = PdfReader(str(path))
            pages = [(page.extract_text() or "") for page in reader.pages]
        except (PdfReadError, OSError, ValueError) as exc:
            raise ParseError(f"could not read PDF {path.name}") from exc

        if not any(page.strip() for page in pages):
            # A PDF with no text layer is a scan. OCR is explicitly out of scope,
            # and silently ingesting nothing would be worse than saying so.
            raise ParseError(
                f"{path.name} contains no extractable text — it is probably scanned, "
                "and OCR is not supported"
            )

        chrome = _repeated_lines(pages)
        blocks: list[Block] = []
        heading: str | None = None

        def current_heading() -> str | None:
            return heading

        for page_number, page_text in enumerate(pages, start=1):
            lines = [
                line.rstrip()
                for line in page_text.splitlines()
                if line.strip() and line.strip() not in chrome
            ]
            index = 0
            paragraph: list[str] = []

            def flush(page: int = page_number) -> None:
                # `heading` is rebound as the page is walked, so it is read at
                # call time via the enclosing scope rather than captured — but
                # `page_number` changes per page, hence the default binding.
                nonlocal paragraph
                if paragraph:
                    text = " ".join(paragraph).strip()
                    if text:
                        blocks.append(
                            Block(
                                type=BlockType.PARAGRAPH,
                                text=text,
                                page=page,
                                heading=current_heading(),
                            )
                        )
                paragraph = []

            while index < len(lines):
                run = _table_run(lines, index)
                if run:
                    flush()
                    blocks.append(
                        Block(
                            type=BlockType.TABLE,
                            text="\n".join(run),
                            page=page_number,
                            heading=heading,
                        )
                    )
                    index += len(run)
                    continue

                line = lines[index]
                if _looks_like_heading(line):
                    flush()
                    heading = line.strip()
                    blocks.append(
                        Block(
                            type=BlockType.HEADING,
                            text=heading,
                            page=page_number,
                            heading=heading,
                        )
                    )
                else:
                    paragraph.append(line)
                index += 1

            flush()

        # The filename is usually the better title for a long PDF. The first
        # heading in a 200-page handbook is whatever page 1 happens to start
        # with — "Section 1" — which tells a reader nothing about the document a
        # citation came from.
        return ParsedDocument(
            blocks=blocks,
            page_count=len(pages),
            title=_document_title(path, blocks, len(pages)),
        )


def _repeated_lines(pages: list[str]) -> set[str]:
    """Lines appearing on most pages: headers, footers, page furniture."""
    if len(pages) < 3:
        # Too few pages to tell repetition from coincidence.
        return set()

    counts: Counter[str] = Counter()
    for page in pages:
        # Count each distinct line once per page, so a word repeated within a
        # page does not look like a header.
        for line in {line.strip() for line in page.splitlines() if line.strip()}:
            counts[line] += 1

    threshold = max(2, int(len(pages) * _REPEAT_THRESHOLD))
    # Page numbers differ per page and so never hit the threshold; they are
    # dropped separately by the digit check.
    return {line for line, count in counts.items() if count >= threshold or line.isdigit()}


def _table_run(lines: list[str], start: int) -> list[str]:
    """The consecutive tabular lines beginning at `start`, or an empty list."""
    run: list[str] = []
    index = start
    while index < len(lines) and _COLUMN_GAP.search(lines[index]):
        run.append(lines[index])
        index += 1
    return run if len(run) >= _MIN_TABLE_ROWS else []


def _looks_like_heading(line: str) -> bool:
    """Short, title-cased, no terminal punctuation — the usual shape of a heading.

    Deliberately conservative: a missed heading costs a little retrieval context,
    while a false positive fragments a paragraph into meaningless pieces.
    """
    stripped = line.strip()
    if len(stripped) > 80 or stripped.endswith((".", ",", ";", ":")):
        return False
    return bool(_HEADING_LIKE.match(stripped))


def _document_title(path: Path, blocks: list[Block], page_count: int) -> str:
    """Pick the most useful title for citation.

    A single-page extract is well described by its first heading. A long document
    is not: page 1's heading describes page 1, while the filename describes the
    whole thing, which is what a citation needs to name.
    """
    first_heading = next((b.text for b in blocks if b.type is BlockType.HEADING), None)
    if page_count > _SHORT_DOCUMENT_PAGES or not first_heading:
        return path.stem
    return first_heading
