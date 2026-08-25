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


def test_a_multi_row_result_is_not_described_as_one() -> None:
    """Found in manual testing: "which ones are offline?" answered "Device-001".

    Five devices were offline. The summary described the first row as though it
    were the answer, which is a wrong answer wearing the shape of a confident
    one — the user has no reason to doubt a named device.
    """
    content = (
        "SQL: SELECT device_id, device_name FROM eaip_curated.v_devices WHERE status = 'offline'\n"
        "\nColumns: device_id | device_name\n"
        "Device-001 | IOT MQTT MONITORING\n"
        "Device-002 | OFFICE MONITORING UNIT\n"
        "Device-003 | Machine-001-001\n"
        "Device-004 | Machine-002-002\n"
        "SmartPole-001 | SMART POLE"
    )

    answer = _summarize_direct_database_result("Which ones are offline?", content, None)

    assert "5 rows" in answer
    # The first device must not be presented as the whole answer.
    assert "IOT MQTT MONITORING" not in answer
