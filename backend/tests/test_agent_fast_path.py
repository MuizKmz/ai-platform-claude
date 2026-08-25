"""Readable summaries for the no-planner single database path."""

from app.api.v1.agent import _summarize_direct_database_result


def test_single_aggregate_is_presented_readably() -> None:
    answer = _summarize_direct_database_result(
        "What was the average server room condition in the last 24 hours?",
        "SQL: SELECT AVG(value)\n\nColumns: average_server_room_condition\n27.38389757",
        None,
    )

    assert answer == "Live IoT result in the last 24 hours: average server room condition: 27.38."


def test_error_is_not_rephrased_as_a_result() -> None:
    answer = _summarize_direct_database_result("What is the latest status?", "", "refused")

    assert answer == "Live data could not answer that question. Review the tool result below."
