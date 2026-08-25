"""Tests for metadata-only semantic layers used by stored SQL integrations."""

from types import SimpleNamespace

from app.connectors.base import QueryResult
from app.connectors.sql.semantic_layer import from_discovered_schema
from app.tools.builtin import _run_iot_device_status_template


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
