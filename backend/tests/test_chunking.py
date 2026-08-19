"""Chunking behaviour. Pure functions, no database, no network."""

from app.knowledge.chunking import chunk_text


def test_empty_input_produces_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_short_document_is_one_chunk() -> None:
    chunks = chunk_text("A short note about refunds.")

    assert len(chunks) == 1
    assert chunks[0].ordinal == 0
    assert chunks[0].content == "A short note about refunds."


def test_headings_become_separate_chunks_and_are_recorded() -> None:
    """Sections are split at headings so one embedding covers one topic."""
    chunks = chunk_text("# Refunds\n\nRefunds take 5 days.\n\n# Shipping\n\nShipping is free.")

    assert [c.heading for c in chunks] == ["Refunds", "Shipping"]
    assert "Refunds take 5 days." in chunks[0].content
    assert "Shipping is free." in chunks[1].content


def test_ordinals_are_contiguous_from_zero() -> None:
    """Ordinal + document_id is the uniqueness constraint, so gaps would break it."""
    chunks = chunk_text("\n\n".join(f"# H{i}\n\nBody {i}." for i in range(6)))

    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_long_text_is_split_and_respects_the_budget() -> None:
    text = " ".join(f"Sentence number {i} about policy." for i in range(400))

    chunks = chunk_text(text, chunk_size=500, overlap=50)

    assert len(chunks) > 1
    # Allow slack for the overlap prefix joined onto the front of a chunk.
    assert all(len(c.content) <= 500 + 50 + 10 for c in chunks)


def test_overlap_repeats_content_across_the_boundary() -> None:
    """A fact on a boundary must be reachable from either side."""
    paragraphs = [f"Paragraph {i} " + "filler " * 30 for i in range(8)]

    chunks = chunk_text("\n\n".join(paragraphs), chunk_size=400, overlap=120)

    assert len(chunks) > 1
    overlapping = sum(
        1
        for i in range(len(chunks) - 1)
        if any(word and word in chunks[i + 1].content for word in chunks[i].content.split()[-6:])
    )
    assert overlapping > 0, "no chunk shared any trailing content with its successor"


def test_a_sentence_longer_than_the_budget_is_still_split() -> None:
    """The last-resort hard cut: without it one long line would exceed the budget."""
    chunks = chunk_text("x" * 5000, chunk_size=400, overlap=40)

    assert len(chunks) > 1
    assert all(len(c.content) <= 400 + 40 + 10 for c in chunks)


def test_plain_text_without_headings_still_chunks() -> None:
    chunks = chunk_text("Line one.\n\nLine two.\n\nLine three.")

    assert len(chunks) >= 1
    assert all(c.heading is None for c in chunks)


def test_overlap_must_be_smaller_than_chunk_size() -> None:
    """Equal values would make the splitter unable to advance — an infinite loop."""
    try:
        chunk_text("some text", chunk_size=100, overlap=100)
    except ValueError as exc:
        assert "overlap" in str(exc)
    else:
        raise AssertionError("expected ValueError")
