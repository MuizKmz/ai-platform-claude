"""NL->SQL accuracy against the live IoT views, measured honestly.

The sibling of `sql_eval.py`, pointed at the connector the pilot actually uses
rather than at a seeded demo warehouse. Three differences, each deliberate:

**It uses the STORED connector.** Built from the `connector` row with its
encrypted credential and its discovered schema — the same objects
`api/v1/agent.py` assembles per request. An eval that constructed its own
connector would measure a code path no user reaches.

**The data moves.** Metrics arrive every minute against a live system, so an
exact expectation for an average is wrong tomorrow. Expectations are exact only
where the fact is stable; a moving number is checked against a band, which is
still a real assertion — a wrong device or a wrong metric falls outside it.

**Refusals matter more here.** The IoT platform has an OEE data gap: the config
table is populated, so the question looks answerable, while the counters stopped
on 2026-06-19. Answering it with June data labelled "last week" would be a
confident lie about a headline manufacturing metric. That single question is
worth more than the rest of the file.

    # tunnel first — the connector reaches MariaDB through it
    ssh -N -L 13306:127.0.0.1:3306 -i ~/.ssh/eaip_iot_tunnel eaip_tunnel@<host>

    cd backend
    uv run python ../evals/iot_eval.py
"""

from __future__ import annotations

import socket
import sys
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.connectors.credentials import decrypt  # noqa: E402
from app.connectors.registry_store import build_connector  # noqa: E402
from app.connectors.sql.connector import SQLConnector  # noqa: E402
from app.connectors.sql.semantic_layer import (  # noqa: E402
    discover_value_hints,
    from_discovered_schema,
)
from app.core.security import Principal  # noqa: E402
from app.db.session import owner_engine  # noqa: E402
from app.llm.providers.openai_provider import OpenAIProvider  # noqa: E402
from app.tools.query_structured_data import query_structured_data  # noqa: E402

QUESTIONS = Path(__file__).parent / "datasets" / "iot_questions.yaml"
CONNECTOR_SLUG = "iot-test"


@dataclass
class Scores:
    total: int = 0
    correct: int = 0
    failures: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def _normalise(value: Any) -> Any:
    """Compare 5 and Decimal('5.00') as equal.

    A model returning numeric where the expectation is int is right about the
    answer; marking it wrong would overstate the error rate.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list | tuple):
        return [_normalise(v) for v in value]
    return value


def _first_number(rows: list[Any]) -> float | None:
    """The single number an aggregate question returns, if it returned one."""
    if not rows or not rows[0]:
        return None
    value = rows[0][0]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _judge(item: dict[str, Any], rows: list[Any]) -> tuple[bool, str]:
    """Did this answer the question? Returns (ok, why-not).

    Four kinds of expectation, because four kinds of question. A live database
    cannot be checked the way a seeded one can, and pretending otherwise would
    mean either brittle tests or no tests.
    """
    if "expected" in item:
        ok = _normalise(item["expected"]) == _normalise(rows)
        return ok, f"expected {item['expected']}, got {rows[:3]}"

    if "between" in item:
        low, high = item["between"]
        value = _first_number(rows)
        if value is None:
            return False, f"expected a number in [{low}, {high}], got {rows[:3]}"
        return low <= value <= high, f"expected [{low}, {high}], got {value}"

    if "expected_row_count" in item:
        want = item["expected_row_count"]
        return len(rows) == want, f"expected {want} rows, got {len(rows)}"

    if "expected_row_count_at_least" in item:
        want = item["expected_row_count_at_least"]
        return len(rows) >= want, f"expected >= {want} rows, got {len(rows)}"

    if "contains" in item:
        needle = str(item["contains"]).lower()
        haystack = " ".join(str(cell).lower() for row in rows for cell in row)
        return needle in haystack, f"expected {needle!r} somewhere in {rows[:3]}"

    return False, "the question carries no expectation"


def _tunnel_is_up(host: str, port: int) -> bool:
    """Fail with the real reason rather than a driver error 30 seconds later."""
    try:
        with socket.create_connection((host, port), timeout=5):
            return True
    except OSError:
        return False


def main() -> int:
    questions = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))

    with Session(owner_engine) as session:
        row = session.execute(
            text("""
                SELECT slug, display_name, settings, credential, required_labels, tenant_id
                FROM connector WHERE slug = :slug
            """),
            {"slug": CONNECTOR_SLUG},
        ).mappings().first()

    if row is None:
        print(f"No connector named {CONNECTOR_SLUG!r}. Add it in the console first.")
        return 2
    if not row["credential"]:
        print(f"Connector {CONNECTOR_SLUG!r} has no credential stored.")
        return 2

    settings_dict = dict(row["settings"] or {})
    host = str(settings_dict.get("host", "127.0.0.1"))
    port = int(settings_dict.get("port", 3306))
    if not _tunnel_is_up(host, port):
        print(f"Nothing is listening on {host}:{port}.")
        print("Start the SSH tunnel first — see the module docstring.")
        return 2

    connector = build_connector(
        kind="sql",
        slug=str(row["slug"]),
        display_name=str(row["display_name"]),
        settings=settings_dict,
        required_labels=tuple(row["required_labels"] or ()),
        credential=decrypt(str(row["credential"])),
    )
    if not isinstance(connector, SQLConnector):
        print("The stored connector is not a SQL connector.")
        return 2

    principal = Principal(
        tenant_id=row["tenant_id"],
        user_id=uuid.uuid4(),
        email="iot-eval@test",
        roles=("reader",),
        allowed_labels=tuple(row["required_labels"] or ()),
    )

    # The reviewed training profile, exactly as the agent reads it: only an
    # ACTIVE one, and only through the same projection. An eval that skipped it
    # would measure a platform the operator is not using.
    with Session(owner_engine) as session:
        profile = session.execute(
            text("""
                SELECT t.submitted_profile FROM training_record t
                JOIN connector c ON c.id = t.connector_id
                WHERE c.slug = :slug AND t.status = 'active'
                ORDER BY t.reviewed_at DESC NULLS LAST LIMIT 1
            """),
            {"slug": CONNECTOR_SLUG},
        ).scalar()

    discovered = connector.describe_schema(principal)
    value_hints = discover_value_hints(connector, principal, discovered)
    semantics = from_discovered_schema(
        discovered,
        connector_name=connector.info.display_name,
        reviewed_training=dict(profile) if isinstance(profile, dict) else None,
        value_hints=value_hints,
    )

    print(f"{len(semantics.views)} approved views, "
          f"{len(value_hints)} column(s) with value hints, "
          f"{len(semantics.iot_metric_templates)} reviewed metric template(s), "
          f"{'a' if profile else 'NO'} reviewed training profile")
    print()

    llm = OpenAIProvider()
    answerable = Scores()
    refusals = Scores()

    for item in questions:
        question = item["question"]
        with Session(owner_engine) as session, session.begin():
            answer = query_structured_data(
                session,
                question=question,
                principal=principal,
                connector=connector,
                semantics=semantics,
                llm=llm,
            )

        if item["kind"] == "unanswerable":
            refusals.total += 1
            if answer.refused:
                refusals.correct += 1
                print(f"  refused   {question}")
            else:
                rows = answer.result.rows if answer.result else []
                refusals.failures.append((question, "answered anyway", str(rows[:2])))
                print(f"  ANSWERED  {question}   <-- should have refused")
            continue

        answerable.total += 1
        if answer.refused or answer.result is None:
            answerable.failures.append((question, answer.error or "refused", ""))
            print(f"  REFUSED   {question}   <-- should have answered")
            continue

        ok, why = _judge(item, answer.result.rows)
        if ok:
            answerable.correct += 1
            print(f"  ok        {question}")
        else:
            answerable.failures.append((question, why, answer.sql or ""))
            print(f"  WRONG     {question}")
            print(f"            {why}")

    print()
    print("=" * 72)
    print(f"Execution accuracy : {answerable.correct}/{answerable.total} "
          f"({answerable.accuracy:.0%})")
    print(f"Refusal accuracy   : {refusals.correct}/{refusals.total} "
          f"({refusals.accuracy:.0%})")
    print("=" * 72)

    if answerable.failures:
        print()
        print("Wrong answers:")
        for question, why, sql in answerable.failures:
            print(f"  {question}")
            print(f"    {why}")
            if sql:
                print(f"    SQL: {sql[:160]}")

    if refusals.failures:
        print()
        print("Should have refused — these are the dangerous ones:")
        for question, why, rows in refusals.failures:
            print(f"  {question}")
            print(f"    {why}: {rows}")

    # Published state of the art on BIRD is ~82% execution accuracy. Anything
    # near 100% here means the questions are too easy, not that this system
    # beats the literature.
    print()
    if answerable.accuracy >= 0.95:
        print("NOTE: >=95% suggests the questions are too easy, not that this is solved.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
