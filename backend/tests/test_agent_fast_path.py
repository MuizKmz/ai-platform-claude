"""Readable summaries for the no-planner single database path."""

from app.api.v1.agent import _summarize_direct_database_result


def test_single_aggregate_is_presented_readably() -> None:
    answer = _summarize_direct_database_result(
        "What was the average server room condition in the last 24 hours?",
        "SQL: SELECT AVG(value)\n\nColumns: average_server_room_condition\n27.38389757",
        None,
    )

    assert answer == "Live result in the last 24 hours: average server room condition: 27.38."


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


def test_a_query_that_matched_nothing_says_so() -> None:
    """ "Live IoT data was retrieved" when none was is a lie with a helpful tone.

    Asked for humidity in the server room — which has temperature and voltage
    sensors and no humidity sensor — the summary said data was retrieved and
    showed none, leaving the user to hunt for an answer that does not exist.
    """
    content = (
        "SQL: SELECT value AS humidity FROM eaip_curated.v_device_metrics "
        "WHERE device_id = 'Device-005' AND metric = 'humidity'\n"
        "\nColumns: humidity\n"
    )

    answer = _summarize_direct_database_result("what's the humidity there?", content, None)

    assert "matched no data" in answer
    assert "retrieved" not in answer


def test_a_null_aggregate_is_an_absence_not_a_measurement() -> None:
    """MAX() over no rows is NULL, and "None°C" reads as a reading of None."""
    content = (
        "SQL: SELECT MAX(value) AS max_temperature_last_24h FROM eaip_curated.v_device_metrics\n"
        "\nColumns: max_temperature_last_24h\nNone"
    )

    answer = _summarize_direct_database_result(
        "and the maximum in the last 24 hours?", content, None
    )

    assert "No matching data" in answer


def test_an_unreachable_connector_is_named_in_the_answer() -> None:
    """Silence here cost three wrong diagnoses in one day.

    With the SSH tunnel down, the connector is skipped, the agent answers from
    documents, and reports the absence as fact: "not available in the provided
    documents". True, useless, and indistinguishable from a platform that knows
    nothing — one of those diagnoses was "maybe the user lacks permission".

    Phase 7's rule: a dead connector produces an explicit "I could not reach
    X", not a hallucinated substitute.
    """
    from types import SimpleNamespace

    from app.api.v1.agent import _note_unreachable

    registry = SimpleNamespace(unreachable_connectors=["IoT test MariaDB"])
    answer = _note_unreachable("The documents do not mention devices.", registry)

    assert answer is not None
    assert "IoT test MariaDB" in answer
    assert "could not be reached" in answer
    # The original answer survives; the warning is added, not substituted.
    assert "The documents do not mention devices." in answer


def test_a_healthy_registry_adds_no_warning() -> None:
    from types import SimpleNamespace

    from app.api.v1.agent import _note_unreachable

    assert _note_unreachable("42 devices.", SimpleNamespace()) == "42 devices."
    assert (
        _note_unreachable("42 devices.", SimpleNamespace(unreachable_connectors=[]))
        == "42 devices."
    )
