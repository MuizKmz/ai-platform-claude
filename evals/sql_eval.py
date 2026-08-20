"""NL->SQL execution accuracy, measured honestly.

Two numbers, reported separately, because they fail differently:

**Execution accuracy** — of the answerable questions, how many returned the
right result? Published state of the art on BIRD is roughly 82% against ~93% for
human experts, so anything near 100% here means the questions are too easy, not
that the system is better than the literature.

**Refusal accuracy** — of the questions this schema cannot answer, how many were
correctly refused? This matters more than it looks. A confident wrong answer to
an unanswerable question is the worst output the system can produce: it is
indistinguishable from a right one, and nobody checks a number that looks fine.

Results are compared by EXECUTION, not by SQL text. Two correct queries can be
written differently, and a query that looks right can be wrong.

    uv run python ../evals/sql_eval.py
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml
from pydantic import SecretStr
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.connectors.egress import EgressPolicy, ResolvedTarget  # noqa: E402
from app.connectors.sql.connector import SQLConnector, SQLConnectorConfig  # noqa: E402
from app.connectors.sql.semantic_layer import ANALYTICS_SEMANTICS  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.security import Principal  # noqa: E402
from app.db.session import owner_engine  # noqa: E402
from app.llm.providers.openai_provider import OpenAIProvider  # noqa: E402
from app.tools.query_structured_data import query_structured_data  # noqa: E402

QUESTIONS = Path(__file__).parent / "datasets" / "sql_questions.yaml"
EVAL_TENANT = uuid.UUID("7b17aaaa-0000-4000-8000-0000000000e5")


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
    answer, and marking it wrong would overstate the error rate.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list | tuple):
        return [_normalise(v) for v in value]
    return value


def _matches(expected: Any, rows: Any) -> bool:
    return _normalise(expected) == _normalise(rows)


def _build_connector() -> SQLConnector:
    config = SQLConnectorConfig(
        id="analytics",
        display_name="Analytics warehouse",
        host=settings.postgres_host,
        port=settings.postgres_port,
        database="analytics",
        username="analytics_readonly",
        password=SecretStr(settings.postgres_readonly_password),
        required_labels=("analytics",),
        egress=EgressPolicy(allow_private=True),
    )
    with patch(
        "app.connectors.sql.connector.resolve_and_validate",
        return_value=ResolvedTarget(host=config.host, ip="127.0.0.1", port=config.port),
    ):
        return SQLConnector(config)


def main() -> int:
    questions = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    connector = _build_connector()
    llm = OpenAIProvider()
    principal = Principal(
        tenant_id=EVAL_TENANT,
        user_id=uuid.uuid4(),
        email="eval@test",
        roles=("reader",),
        allowed_labels=("analytics",),
    )

    # The audit table has a foreign key to tenant, and every attempt is audited.
    with Session(owner_engine) as session, session.begin():
        session.execute(
            __import__("sqlalchemy").text(
                "INSERT INTO tenant (id, slug, name) VALUES (:t, 'sql-eval', 'SQL Eval') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"t": EVAL_TENANT},
        )

    answerable = Scores()
    refusals = Scores()

    for item in questions:
        with Session(owner_engine) as session, session.begin():
            answer = query_structured_data(
                session,
                question=item["question"],
                principal=principal,
                connector=connector,
                semantics=ANALYTICS_SEMANTICS,
                llm=llm,
            )

        if item["kind"] == "unanswerable":
            refusals.total += 1
            if answer.refused:
                refusals.correct += 1
            else:
                rows = answer.result.rows if answer.result else ()
                refusals.failures.append(
                    (item["question"], answer.sql or "", f"answered instead of refusing: {rows}")
                )
            continue

        answerable.total += 1
        if answer.refused:
            answerable.failures.append(
                (item["question"], answer.sql or "", f"refused: {answer.error}")
            )
        elif answer.result and _matches(item["expected"], [list(r) for r in answer.result.rows]):
            answerable.correct += 1
        else:
            rows = [list(r) for r in answer.result.rows] if answer.result else []
            answerable.failures.append(
                (item["question"], answer.sql or "", f"expected {item['expected']}, got {rows}")
            )

    connector.dispose()

    print(f"\nModel: {llm.model}\n")
    print(f"Execution accuracy : {answerable.accuracy:.0%}  "
          f"({answerable.correct}/{answerable.total} answerable questions)")
    print(f"Refusal accuracy   : {refusals.accuracy:.0%}  "
          f"({refusals.correct}/{refusals.total} unanswerable questions)")

    for label, scores in (("EXECUTION", answerable), ("REFUSAL", refusals)):
        if scores.failures:
            print(f"\n{label} failures:")
            for question, sql, reason in scores.failures:
                print(f"\n  Q: {question}")
                print(f"     {reason}")
                if sql:
                    print(f"     SQL: {sql[:120]}")

    print(
        "\nFor calibration: published state of the art on BIRD is ~82% execution "
        "accuracy,\nagainst ~93% for human experts. Text-to-SQL is a drafting aid, "
        "not an oracle."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
