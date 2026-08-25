"""Routing tests that do not require an LLM or a database."""

from app.api.v1.agent import _question_with_context, needs_agent


def test_operational_status_question_uses_the_agent() -> None:
    assert needs_agent("Which devices are currently offline?")


def test_document_question_stays_on_the_lower_cost_direct_path() -> None:
    assert not needs_agent("What does the refund policy say?")


def test_trained_business_term_with_relative_time_uses_the_agent() -> None:
    assert needs_agent("What was the server room condition in the last 24 hours?")


# Regression coverage for the bug report: "what is the maximum" was routed to
# document RAG and answered "I don't have enough information," right after an
# identically-shaped "what is the minimum ... of that device?" worked. The
# minimum question only worked because it matched "device", not "minimum" —
# _DATA_SHAPE had no aggregate/superlative words besides "average". Assert the
# whole vocabulary directly so a future edit can't drop one silently.
def test_all_aggregate_and_superlative_questions_use_the_agent() -> None:
    for word in [
        "minimum",
        "maximum",
        "average",
        "mean",
        "median",
        "peak",
        "highest",
        "lowest",
        "smallest",
        "largest",
        "biggest",
        "range",
    ]:
        question = f"What is the {word} temperature?"
        assert needs_agent(question), f"{word!r} should route to the agent: {question!r}"


def test_bare_maximum_follow_up_with_no_named_device_uses_the_agent() -> None:
    # The exact failing message from the bug report, with no device noun at all.
    assert needs_agent("what is the maximum")


def test_bare_minimum_follow_up_with_no_named_device_uses_the_agent() -> None:
    assert needs_agent("what is the minimum")


# _question_with_context: elliptical follow-ups should inherit the previous
# device result even without the literal phrase "that device".
def test_bare_follow_up_inherits_previous_device_context() -> None:
    context = "SERVER ROOM UNIT (device ID Device-005)"
    result = _question_with_context("what is the maximum", context)
    assert "Device-005" in result
    assert "what is the maximum" in result


def test_pronoun_follow_up_inherits_previous_device_context() -> None:
    context = "SERVER ROOM UNIT (device ID Device-005)"
    result = _question_with_context("is it online right now?", context)
    assert "Device-005" in result


def test_explicit_that_device_still_inherits_context() -> None:
    context = "SERVER ROOM UNIT (device ID Device-005)"
    result = _question_with_context("What is the minimum temperature of that device?", context)
    assert "Device-005" in result


def test_unrelated_question_does_not_inherit_stale_device_context() -> None:
    context = "SERVER ROOM UNIT (device ID Device-005)"
    result = _question_with_context("What does the refund policy say?", context)
    assert result == "What does the refund policy say?"


def test_no_context_leaves_question_unchanged() -> None:
    assert _question_with_context("what is the maximum", None) == "what is the maximum"


def test_its_is_a_follow_up_reference() -> None:
    """The most natural follow-up there is, and it did not match.

    `\bit\b` does not match "its" — the word boundary falls after the s. So
    "What's its temperature right now?" carried no device, reached no tool, and
    answered "I don't have enough information" one turn after the device was
    named on screen.
    """
    context = "SERVER ROOM UNIT (device ID Device-005)"

    for question in (
        "What's its temperature right now?",
        "whats its tempertaure right now?",
        "What is its humidity?",
        "How hot is it in there?",
    ):
        combined = _question_with_context(question, context)
        assert "Device-005" in combined, f"no device context attached to {question!r}"


def test_a_bare_measurement_question_reaches_the_database() -> None:
    """ "What is its temperature right now" named no aggregate, so nothing in
    _DATA_SHAPE matched and the question never reached a tool. The metric names
    themselves are what a person actually says."""
    for question in (
        "What's its temperature right now?",
        "What is the humidity in the server room?",
        "How hot is the office?",
        "What is the voltage on that device?",
    ):
        assert needs_agent(question), f"{question!r} would not reach a database tool"
