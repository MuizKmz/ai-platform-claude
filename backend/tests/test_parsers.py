"""Document parsing and table-aware chunking.

Fixtures are generated on demand by tests/fixtures/make_fixtures.py, so a fresh
clone has no binary blobs to review and no missing-file failures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.knowledge.chunking import chunk_blocks
from app.knowledge.parsers import BlockType, ParseError, parse_document, parser_for
from tests.fixtures.make_fixtures import PAGE_HEADER, PART_TABLE
from tests.fixtures.make_fixtures import main as build_fixtures

DOCUMENTS = Path(__file__).parent / "fixtures" / "documents"

FIRST_PART = PART_TABLE[1][0]  # XR-7742-B
LAST_PART = PART_TABLE[-1][0]  # MB-4460-D


@pytest.fixture(scope="session", autouse=True)
def fixtures() -> None:
    if not (DOCUMENTS / "handbook.pdf").exists():
        build_fixtures()


@pytest.mark.parametrize(
    "filename", ["catalogue.md", "catalogue.html", "catalogue.docx", "handbook.pdf"]
)
def test_every_format_parses_into_blocks(filename: str) -> None:
    parsed = parse_document(DOCUMENTS / filename)

    assert parsed.blocks, f"{filename} produced no blocks"
    assert parsed.text.strip()


@pytest.mark.parametrize(
    "filename", ["catalogue.md", "catalogue.html", "catalogue.docx", "handbook.pdf"]
)
def test_tables_are_detected_as_atomic_blocks(filename: str) -> None:
    parsed = parse_document(DOCUMENTS / filename)

    tables = [b for b in parsed.blocks if b.type is BlockType.TABLE]

    assert len(tables) == 1, f"{filename}: expected exactly one table block"
    assert tables[0].is_atomic


@pytest.mark.parametrize(
    "filename", ["catalogue.md", "catalogue.html", "catalogue.docx", "handbook.pdf"]
)
def test_table_is_not_split_across_chunks(filename: str) -> None:
    """The roadmap's headline requirement.

    First and last rows must land in the SAME chunk. A table split at row 3
    leaves the remaining rows as numbers whose column names are in another chunk.
    """
    parsed = parse_document(DOCUMENTS / filename)

    chunks = chunk_blocks(parsed.blocks, chunk_size=200, overlap=20)

    holding_first = [c for c in chunks if FIRST_PART in c.content]
    assert len(holding_first) == 1, f"{filename}: table row appears in {len(holding_first)} chunks"
    assert LAST_PART in holding_first[0].content, f"{filename}: table was split"
    # The header row must travel with the data.
    assert PART_TABLE[0][0] in holding_first[0].content


def test_table_survives_even_when_larger_than_chunk_size() -> None:
    """An oversized table is a worse chunk than an oversized paragraph, and a far
    better one than half a table."""
    parsed = parse_document(DOCUMENTS / "catalogue.md")

    chunks = chunk_blocks(parsed.blocks, chunk_size=50, overlap=10)

    table_chunks = [c for c in chunks if FIRST_PART in c.content]
    assert len(table_chunks) == 1
    assert LAST_PART in table_chunks[0].content


# --- PDF specifics ------------------------------------------------------------


def test_pdf_reports_page_count() -> None:
    parsed = parse_document(DOCUMENTS / "handbook.pdf")

    assert parsed.page_count == 200


def test_repeated_page_header_is_stripped() -> None:
    """A header on 200 pages would otherwise become 200 near-identical chunks
    that crowd real content out of the top-k."""
    parsed = parse_document(DOCUMENTS / "handbook.pdf")

    occurrences = sum(1 for block in parsed.blocks if PAGE_HEADER in block.text)

    assert occurrences == 0, f"page header survived in {occurrences} blocks"


def test_pdf_blocks_carry_page_numbers() -> None:
    """Needed for citation back to a location a human can find."""
    parsed = parse_document(DOCUMENTS / "handbook.pdf")

    pages = {block.page for block in parsed.blocks if block.page is not None}

    assert len(pages) > 100
    assert min(pages) == 1
    assert max(pages) == 200


def test_scanned_pdf_is_refused_rather_than_silently_empty(tmp_path: Path) -> None:
    """A PDF with no text layer must fail loudly.

    Ingesting nothing and reporting success is the worse outcome: the document
    appears in the corpus and answers no question.
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    blank = tmp_path / "scanned.pdf"
    pdf = canvas.Canvas(str(blank), pagesize=LETTER)
    pdf.showPage()
    pdf.save()

    with pytest.raises(ParseError, match="scanned"):
        parse_document(blank)


# --- HTML specifics -----------------------------------------------------------


def test_html_navigation_and_footer_are_discarded() -> None:
    """Site chrome appears on every page and never answers a question."""
    parsed = parse_document(DOCUMENTS / "catalogue.html")

    text = parsed.text

    assert "Home | Products | Contact" not in text
    assert "Copyright 2026" not in text
    assert "procurement portal" in text


def test_html_title_is_used() -> None:
    parsed = parse_document(DOCUMENTS / "catalogue.html")

    assert parsed.title == "Parts Catalogue"


# --- DOCX specifics -----------------------------------------------------------


def test_docx_preserves_document_order() -> None:
    """python-docx exposes paragraphs and tables separately, which loses their
    relative order; the parser walks the body XML to keep it."""
    parsed = parse_document(DOCUMENTS / "catalogue.docx")

    types = [b.type for b in parsed.blocks]
    table_index = types.index(BlockType.TABLE)
    ordering_index = next(i for i, b in enumerate(parsed.blocks) if b.text == "Ordering")

    assert table_index < ordering_index, "the table drifted past a later heading"


# --- dispatch -----------------------------------------------------------------


def test_unsupported_format_is_refused(tmp_path: Path) -> None:
    unsupported = tmp_path / "image.png"
    unsupported.write_bytes(b"\x89PNG\r\n")

    assert parser_for(unsupported) is None
    with pytest.raises(ParseError, match="unsupported"):
        parse_document(unsupported)


def test_headings_are_not_emitted_as_their_own_chunks() -> None:
    """A heading alone is a chunk whose entire body is three words; it retrieves
    against everything and answers nothing."""
    parsed = parse_document(DOCUMENTS / "catalogue.md")

    chunks = chunk_blocks(parsed.blocks)

    assert not any(c.content.strip() == "Ordering" for c in chunks)
    # But the heading survives as context on the chunk that follows it.
    assert any(c.heading == "Ordering" for c in chunks)


def test_long_pdf_is_titled_by_filename_not_its_first_heading() -> None:
    """A citation must name the document, not page 1 of it.

    The first heading in a 200-page handbook is whatever page 1 starts with —
    "Section 1" — which tells a reader nothing about where a fact came from.
    """
    parsed = parse_document(DOCUMENTS / "handbook.pdf")

    assert parsed.title == "handbook"
