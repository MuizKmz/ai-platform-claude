"""Tests for metadata-only semantic layers used by stored SQL integrations."""

from types import SimpleNamespace

from app.connectors.base import QueryResult
from app.connectors.sql.semantic_layer import (
    MAX_HINTED_VALUES,
    discover_value_hints,
    from_discovered_schema,
)
from app.tools.builtin import _run_iot_device_status_template


def _connector_returning(values_by_column: dict[str, list[str]]) -> SimpleNamespace:
    """A connector that answers DISTINCT queries from a canned mapping."""

    def query(_principal: object, sql: str) -> QueryResult:
        for column, values in values_by_column.items():
            if f"SELECT DISTINCT {column} " in sql:
                return QueryResult(
                    (column,), tuple((v,) for v in values), sql, len(values), False, 1.0
                )
        return QueryResult((), (), sql, 0, False, 1.0)

    return SimpleNamespace(query=query)


def test_value_hints_reach_the_prompt() -> None:
    """The fix for the failure that started this: `status = 'down'`.

    Asked "what machines are down", the model matched the user's word against a
    column holding online/offline and returned zero rows — which reads as
    "nothing is down" while five devices were. Naming the values moved the IoT
    eval from 83% to 100% execution accuracy.
    """
    discovered = [
        {
            "name": "curated.v_devices",
            "columns": [{"name": "status", "type": "varchar"}],
        }
    ]
    connector = _connector_returning({"status": ["online", "offline"]})

    hints = discover_value_hints(connector, object(), discovered)
    assert hints == {"curated.v_devices.status": ("offline", "online")}

    semantics = from_discovered_schema(discovered, connector_name="IoT", value_hints=hints)
    prompt = semantics.to_prompt()
    assert "'offline'" in prompt
    assert "'online'" in prompt


def test_only_enumeration_columns_are_read() -> None:
    """An allowlist, not a cardinality test.

    "Few distinct values" also describes a table of six customers. Enumerating
    people into a prompt is exactly what this must never do, so the gate is the
    column NAME — device_name is not on the list however few there are.
    """
    discovered = [
        {
            "name": "curated.v_devices",
            "columns": [
                {"name": "status", "type": "varchar"},
                {"name": "device_name", "type": "varchar"},
                {"name": "email", "type": "varchar"},
            ],
        }
    ]
    connector = _connector_returning(
        {
            "status": ["online"],
            "device_name": ["SERVER ROOM UNIT"],
            "email": ["alice@acme.test"],
        }
    )

    hints = discover_value_hints(connector, object(), discovered)

    assert "curated.v_devices.status" in hints
    assert "curated.v_devices.device_name" not in hints
    assert "curated.v_devices.email" not in hints


def test_a_column_with_too_many_values_is_dropped_not_truncated() -> None:
    """A truncated list is worse than none: the model treats it as complete."""
    discovered = [
        {
            "name": "curated.v_metrics",
            "columns": [{"name": "metric", "type": "varchar"}],
        }
    ]
    connector = _connector_returning(
        {"metric": [f"metric_{i}" for i in range(MAX_HINTED_VALUES + 1)]}
    )

    assert discover_value_hints(connector, object(), discovered) == {}


def test_a_failing_column_does_not_break_discovery() -> None:
    """A hint is an optimisation. Losing one costs accuracy; raising costs the
    whole connector, and with it every question the user could have asked."""
    discovered = [
        {
            "name": "curated.v_devices",
            "columns": [
                {"name": "status", "type": "varchar"},
                {"name": "level", "type": "varchar"},
            ],
        }
    ]

    def query(_principal: object, sql: str) -> QueryResult:
        if "level" in sql:
            raise RuntimeError("permission denied")
        return QueryResult(("status",), (("online",),), sql, 1, False, 1.0)

    hints = discover_value_hints(SimpleNamespace(query=query), object(), discovered)

    assert hints == {"curated.v_devices.status": ("online",)}


def test_hints_are_scoped_to_their_view() -> None:
    """`status` means different things in two views; a hint from one would be a
    lie in the other."""
    discovered = [
        {"name": "curated.v_devices", "columns": [{"name": "status", "type": "varchar"}]},
        {"name": "curated.v_jobs", "columns": [{"name": "status", "type": "varchar"}]},
    ]
    semantics = from_discovered_schema(
        discovered,
        connector_name="IoT",
        value_hints={"curated.v_devices.status": ("online", "offline")},
    )

    devices = next(v for v in semantics.views if v.name == "curated.v_devices")
    jobs = next(v for v in semantics.views if v.name == "curated.v_jobs")

    assert "'online'" in devices.columns[0].description
    assert "'online'" not in jobs.columns[0].description


def test_discovered_schema_becomes_promptable_approved_views() -> None:
    semantics = from_discovered_schema(
        [
            {
                "name": "eaip_curated.v_devices",
                "columns": [
                    {"name": "device_id", "type": "varchar"},
                    {"name": "status", "type": "enum"},
                ],
            },
            {"name": "eaip_curated.empty", "columns": []},
            {"not_a_view": "ignored"},
        ],
        connector_name="IoT test MariaDB",
    )

    assert [view.name for view in semantics.views] == ["eaip_curated.v_devices"]
    prompt = semantics.to_prompt()
    assert "IoT test MariaDB" in prompt
    assert "device_id" in prompt
    assert "Source data type: enum." in prompt
    assert "sampled business rows" in prompt


def test_reviewed_iot_term_creates_only_an_explicit_metric_template() -> None:
    semantics = from_discovered_schema(
        [
            {"name": "eaip_curated.v_devices", "columns": [{"name": "device_id"}]},
            {
                "name": "eaip_curated.v_device_metrics",
                "columns": [{"name": "metric"}, {"name": "value"}],
            },
        ],
        connector_name="IoT test MariaDB",
        reviewed_training={
            "metrics": [{"name": "temperature", "definition": "Degrees Celsius."}],
            "business_terms": [
                {
                    "term": "server room condition",
                    "synonyms": ["server room temperature"],
                    "definition": (
                        "For reporting this means the temperature metric recorded "
                        "for the device named Server Room Unit."
                    ),
                }
            ],
        },
    )

    assert len(semantics.iot_metric_templates) == 1
    template = semantics.iot_metric_templates[0]
    assert template.device_name == "Server Room Unit"
    assert template.metric == "temperature"
    assert "server room temperature" in template.aliases


def test_online_device_question_uses_the_approved_view_without_an_llm() -> None:
    semantics = from_discovered_schema(
        [
            {
                "name": "eaip_curated.v_devices",
                "columns": [
                    {"name": "device_id"},
                    {"name": "device_name"},
                    {"name": "status"},
                ],
            }
        ],
        connector_name="IoT test MariaDB",
    )
    captured: dict[str, str] = {}

    def query(_principal: object, sql: str) -> QueryResult:
        captured["sql"] = sql
        return QueryResult((), (), sql, 0, False, 1.0)

    connector = SimpleNamespace(query=query)
    result = _run_iot_device_status_template(  # type: ignore[arg-type]
        connector,
        semantics,
        object(),  # type: ignore[arg-type]
        "What devices are online?",
    )

    assert isinstance(result, QueryResult)
    assert "FROM eaip_curated.v_devices" in captured["sql"]
    assert "status = 'online'" in captured["sql"]
