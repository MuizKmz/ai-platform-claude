"""PII detection and redaction.

Includes the roadmap's `test_pii_redacted_in_traces`.

Three things are asserted, and the third is the one that matters most:

  - **Detection is accurate enough to be trusted.** A detector that flags every
    sixteen-digit order number is one people learn to ignore, so the false
    positives are tested as carefully as the true ones.

  - **Ingestion records but does not refuse.** Enterprise documents contain
    people. Blocking them would refuse the product; counting them makes the
    labelling decision informed.

  - **Redaction happens at the choke point.** `Span.set_attribute` is the one
    place every trace attribute passes through. Testing it there rather than at
    each call site is what makes the guarantee hold for call sites that do not
    exist yet — and there will be more every phase.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from app.core.config import settings
from app.core.pii import PIIKind, contains_pii, find, redact, summarise
from app.observability.tracing import Span, trace


def _database_available() -> bool:
    try:
        engine = create_engine(settings.database_url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.security

TENANT = uuid.UUID("9911e000-0000-0000-0000-000000000099")

# Formats, not real values. Every one of these is a documented test constant:
# 4111... is Visa's published test number, 555-01xx is the reserved fictional
# US phone range.
EMAIL = "alice.smith+billing@acme-corp.co.uk"
CARD = "4111 1111 1111 1111"
SSN = "123-45-6789"
PHONE = "+1 555-123-4567"
API_KEY = "sk-abcdefghijklmnopqrstuvwxyz1234"  # noqa: S105 — a shaped fixture


# --- detection accuracy -------------------------------------------------------


@pytest.mark.parametrize(
    ("text_", "kind"),
    [
        (f"Contact {EMAIL} for billing", PIIKind.EMAIL),
        (f"Card on file: {CARD}", PIIKind.CREDIT_CARD),
        (f"SSN {SSN} on record", PIIKind.SSN),
        (f"Reach us at {PHONE}", PIIKind.PHONE),
        (f"Use {API_KEY} to authenticate", PIIKind.API_KEY),
        ("Server 192.168.1.50 is down", PIIKind.IP_ADDRESS),
        ("Account GB29NWBK60161331926819", PIIKind.IBAN),
    ],
)
def test_each_kind_is_detected(text_: str, kind: PIIKind) -> None:
    kinds = {match.kind for match in find(text_)}
    assert kind in kinds, f"{kind} was not detected in {text_!r}"


@pytest.mark.parametrize(
    "text_",
    [
        # The one that matters most. A sixteen-digit order number is far more
        # common in this corpus than a card, and flagging every one of them
        # would make the whole detector noise.
        "Order 1234567890123456 shipped on Tuesday",
        "Build 10.0.26200.1 was released",
        "Refunds are processed within five business days of approval.",
        "Section 4.2.1 covers expense claims",
        "The meeting is at 2:30 on 2026-01-15",
    ],
)
def test_ordinary_text_is_not_flagged(text_: str) -> None:
    """False positives are a real cost: a detector people ignore protects
    nothing."""
    assert not contains_pii(text_), f"false positive in {text_!r}: {summarise(text_)}"


def test_a_card_number_must_pass_luhn() -> None:
    """Shape alone is not enough. The published Visa test number validates;
    the same digits with one changed does not."""
    assert contains_pii("Card 4111 1111 1111 1111")
    assert not contains_pii("Card 4111 1111 1111 1112")


def test_counts_are_returned_never_values() -> None:
    """A summary carrying examples would put the PII into the very metadata
    written to make it visible — the same mistake as logging a password to
    prove it was rejected."""
    summary = summarise(f"{EMAIL} and {SSN} and {EMAIL}")

    assert summary == {"email": 2, "ssn": 1}
    serialised = str(summary)
    assert EMAIL not in serialised
    assert SSN not in serialised


# --- redaction ----------------------------------------------------------------


def test_redaction_removes_the_value_and_keeps_the_shape() -> None:
    """Typed placeholders: `[EMAIL]` says a message contained an address
    without saying whose, and leaves enough structure to debug with."""
    redacted = redact(f"Please email {EMAIL} about card {CARD}")

    assert EMAIL not in redacted
    assert "4111" not in redacted
    assert "[EMAIL]" in redacted
    assert "[CREDIT_CARD]" in redacted
    # Surrounding text survives intact, including the spaces around a match —
    # an earlier version of the card pattern swallowed the following space.
    assert redacted == "Please email [EMAIL] about card [CREDIT_CARD]"


def test_redaction_leaves_clean_text_alone() -> None:
    clean = "Refunds are processed within five business days."
    assert redact(clean) == clean


def test_overlapping_matches_report_the_more_alarming_kind() -> None:
    """Patterns are ordered most-alarming first, so a leaked credential is not
    reported as something duller that happens to match the same span."""
    kinds = {m.kind for m in find(f"token={API_KEY}")}
    assert PIIKind.API_KEY in kinds


# --- the roadmap's named test -------------------------------------------------


def test_pii_redacted_in_traces() -> None:
    """Nothing written to a span may carry PII.

    Asserted against `set_attribute` rather than against a particular endpoint,
    because that method is the choke point every attribute passes through. A
    per-endpoint test would prove today's call sites are careful and say nothing
    about the ones added next phase.
    """
    span = Span(trace_id="t", span_id="s", name="test")

    span.set_attribute("question", f"What is the status for {EMAIL}?")
    span.set_attribute("note", f"card {CARD} declined")

    serialised = str(span.attributes)
    assert EMAIL not in serialised
    assert "4111" not in serialised
    assert "[EMAIL]" in span.attributes["question"]
    assert "[CREDIT_CARD]" in span.attributes["note"]


def test_non_string_attributes_pass_through_unchanged() -> None:
    """Counts, durations, and costs are what belongs in a span. Coercing them
    to strings to scan them would corrupt the numbers the trace exists for."""
    span = Span(trace_id="t", span_id="s", name="test")

    span.set_attribute("result_count", 4)
    span.set_attribute("cost_usd", 0.000216)
    span.set_attribute("refused", False)

    assert span.attributes == {"result_count": 4, "cost_usd": 0.000216, "refused": False}


@pytest.mark.skipif(not _database_available(), reason="no database reachable")
def test_a_persisted_span_carries_no_pii() -> None:
    """End to end: through the real trace() context manager and into Postgres.

    The unit test above proves the method redacts. This proves nothing between
    it and the database puts the value back.
    """
    from sqlalchemy.orm import Session

    engine = create_engine(settings.database_url)
    trace_id = uuid.uuid4().hex

    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": TENANT})
            conn.execute(
                text("INSERT INTO tenant (id, slug, name) VALUES (:t, 'pii-test', 'PII')"),
                {"t": TENANT},
            )

        with Session(engine) as session:
            with trace(session, "pii.check", tenant_id=TENANT, trace_id=trace_id) as span:
                span.set_attribute("question", f"Refund for {EMAIL}?")
            session.commit()

        with engine.connect() as conn:
            stored = conn.execute(
                text("SELECT attributes::text FROM trace_span WHERE trace_id = :t"),
                {"t": trace_id},
            ).scalar()

        assert stored is not None
        assert EMAIL not in stored
        assert "[EMAIL]" in stored
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM trace_span WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": TENANT})
        engine.dispose()


# --- ingestion ----------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.mark.skipif(not _database_available(), reason="no database reachable")
def test_ingestion_records_pii_without_refusing_the_document(
    engine: Engine, tmp_path: object
) -> None:
    """A document full of customer addresses must still be ingestible.

    This platform answers questions about enterprise documents, and enterprise
    documents contain people. Refusing them would refuse the product; the label
    system is the control, and the recorded counts make applying it informed.
    """
    from pathlib import Path

    from sqlalchemy.orm import Session

    from app.knowledge.embedding import FakeEmbeddings
    from app.knowledge.ingest import IngestResult, ingest_file

    directory = Path(str(tmp_path))
    document = directory / "support-export.md"
    document.write_text(
        "# Support export\n\n"
        f"Ticket 1 from {EMAIL}\n\n"
        f"Ticket 2 from bob@example.org, phone {PHONE}\n",
        encoding="utf-8",
    )

    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": TENANT})
            conn.execute(
                text("INSERT INTO tenant (id, slug, name) VALUES (:t, 'pii-ing', 'PII')"),
                {"t": TENANT},
            )

        with Session(engine) as session:
            result = IngestResult()
            ingest_file(
                session,
                path=document,
                tenant_id=TENANT,
                labels=["confidential"],
                provider=FakeEmbeddings(),
                result=result,
            )
            session.commit()

        # Ingested, not refused.
        assert result.documents_created == 1

        with engine.connect() as conn:
            summary = conn.execute(
                text("SELECT pii_summary FROM document WHERE tenant_id = :t"),
                {"t": TENANT},
            ).scalar()

        assert summary, "the scan recorded nothing"
        assert summary.get("email") == 2
        assert summary.get("phone") == 1
        # Counts, never values.
        assert EMAIL not in str(summary)
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM chunk WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM document WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": TENANT})


@pytest.mark.skipif(not _database_available(), reason="no database reachable")
def test_a_clean_document_records_an_empty_summary(engine: Engine, tmp_path: object) -> None:
    """The complement, so the test above is not satisfied by recording
    something for every document."""
    from pathlib import Path

    from sqlalchemy.orm import Session

    from app.knowledge.embedding import FakeEmbeddings
    from app.knowledge.ingest import IngestResult, ingest_file

    document = Path(str(tmp_path)) / "policy.md"
    document.write_text(
        "# Refund policy\n\nRefunds are processed within five business days.\n",
        encoding="utf-8",
    )

    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": TENANT})
            conn.execute(
                text("INSERT INTO tenant (id, slug, name) VALUES (:t, 'pii-cln', 'PII')"),
                {"t": TENANT},
            )

        with Session(engine) as session:
            result = IngestResult()
            ingest_file(
                session,
                path=document,
                tenant_id=TENANT,
                labels=["public"],
                provider=FakeEmbeddings(),
                result=result,
            )
            session.commit()

        with engine.connect() as conn:
            summary = conn.execute(
                text("SELECT pii_summary FROM document WHERE tenant_id = :t"),
                {"t": TENANT},
            ).scalar()

        assert summary == {}
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM chunk WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM document WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": TENANT})
