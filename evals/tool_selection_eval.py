"""Tool-selection accuracy, measured with a real model.

Scored by the SET of tools used, not the sequence. Order is rarely determined
by the question, and grading it would fail runs that were right for reasons the
grader did not anticipate.

Reported per category, because they fail differently and an aggregate hides it:

    knowledge — over-reaching for the database on a documentation question
    database  — searching documents for a number that is not written down
    both      — the case the agent exists for; using one tool is a wrong answer
                that looks like a right one
    none      — the corpus is silent, so a REFUSAL is the correct outcome.
                Graded on the answer rather than on tool use: an agent cannot
                know a corpus lacks something without looking, so searching
                first and then refusing is right, not wrong. Grading tool use
                here scored a correctly-refusing agent at 0%.

Costs real money — roughly a cent per full run — so it is a script you invoke
deliberately rather than a test CI executes.

    uv run python ../evals/tool_selection_eval.py
"""

from __future__ import annotations

import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import SecretStr
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.agent.graph import run_agent  # noqa: E402
from app.agent.limits import RunLimits  # noqa: E402
from app.connectors.egress import EgressPolicy  # noqa: E402
from app.connectors.sql.connector import SQLConnector, SQLConnectorConfig  # noqa: E402
from app.connectors.sql.semantic_layer import ANALYTICS_SEMANTICS  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.security import Principal  # noqa: E402
from app.db.session import owner_engine  # noqa: E402
from app.knowledge.embedding import get_embedding_provider  # noqa: E402
from app.llm.providers.openai_provider import OpenAIProvider  # noqa: E402
from app.tools.base import ToolRegistry  # noqa: E402
from app.tools.builtin import QueryDatabaseTool, SearchKnowledgeTool  # noqa: E402

QUESTIONS = Path(__file__).parent / "datasets" / "tool_selection.yaml"

# The tenant whose corpus the eval reads. Uses the real acme tenant rather than
# a fixture, because tool selection depends on what is actually retrievable —
# an empty corpus would make every knowledge question look like a refusal.
EVAL_TENANT_SLUG = "acme"


@dataclass
class Scores:
    total: int = 0
    correct: int = 0
    failures: list[tuple[str, set[str], set[str]]] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


# Phrases an agent uses when it has nothing to answer with. Matched loosely
# because the wording varies run to run and the substance does not.
_REFUSAL_MARKERS = (
    "not available",
    "no available",
    "no information",
    "not enough information",
    "no relevant",
    "does not provide",
    "not specified",
    "no mention",
    "does not contain",
    "could not find",
    "not found",
    "cannot determine",
    "don't have enough",
    "do not have enough",
    "unable to",
    "no data",
    "not present",
)


def _refused(answer: str | None) -> bool:
    """Whether an answer declines rather than asserts."""
    if not answer:
        # A halt with no answer is also a refusal — the run stopped without
        # inventing anything, which is the property being measured.
        return True
    lowered = answer.lower()
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


def _resolve_tenant() -> uuid.UUID:
    from sqlalchemy import text

    with Session(owner_engine) as session:
        found = session.execute(
            text("SELECT id FROM tenant WHERE slug = :slug"), {"slug": EVAL_TENANT_SLUG}
        ).scalar()
    if not found:
        raise SystemExit(f"tenant {EVAL_TENANT_SLUG!r} not found; create it with the CLI")
    return uuid.UUID(str(found))


def _build_registry(session: Session, tenant_id: uuid.UUID) -> ToolRegistry:
    llm = OpenAIProvider()
    registry = ToolRegistry()

    registry.register(
        tenant_id, SearchKnowledgeTool(session, get_embedding_provider(), ("public",))
    )

    connector = SQLConnector(
        SQLConnectorConfig(
            id="analytics",
            display_name="Analytics",
            host=settings.postgres_host,
            port=settings.postgres_port,
            database="analytics",
            username="analytics_readonly",
            password=SecretStr(settings.postgres_readonly_password),
            required_labels=("analytics",),
            egress=EgressPolicy(allow_private=True, allow_loopback=True),
        )
    )
    registry.register(
        tenant_id,
        QueryDatabaseTool(session, connector, ANALYTICS_SEMANTICS, llm, ("analytics",)),
    )
    return registry


def main() -> int:
    questions = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    tenant_id = _resolve_tenant()

    principal = Principal(
        tenant_id=tenant_id,
        user_id=uuid.uuid4(),
        email="eval@test",
        roles=("reader",),
        # Both labels: the eval measures whether the model CHOOSES correctly,
        # not whether authorization works. That has its own tests.
        allowed_labels=("public", "analytics"),
    )

    llm = OpenAIProvider()
    by_kind: dict[str, Scores] = defaultdict(Scores)
    total_cost = 0.0

    with Session(owner_engine) as session:
        registry = _build_registry(session, tenant_id)

        for item in questions:
            expected = set(item["expects"])
            kind = item["kind"]

            run = run_agent(
                item["question"],
                principal=principal,
                registry=registry,
                llm=llm,
                # Tighter than production: a run needing more than four steps
                # for these questions has misunderstood, and letting it
                # continue costs money without changing the verdict.
                limits=RunLimits(max_steps=4, max_tool_calls=4),
            )
            total_cost += run.cost_usd

            # Denied calls do not count as selections: the model asked, the
            # platform refused, and nothing ran.
            used = {
                call.tool
                for call in run.tool_calls
                if not call.metadata.get("denied")
            }

            score = by_kind[kind]
            score.total += 1

            if kind == "none":
                # Graded on the OUTCOME. An agent cannot know the corpus is
                # silent without looking, so searching and then refusing is
                # correct behaviour — the failure would be answering anyway.
                correct = _refused(run.answer)
                observed = {"answered"} if not correct else {"refused"}
            else:
                correct = used == expected
                observed = used

            if correct:
                score.correct += 1
            else:
                score.failures.append((item["question"], expected, observed))

    print(f"\nModel: {llm.model}\n")
    header = f"{'category':<12}{'accuracy':>10}{'':>4}{'n':>4}"
    print(header)
    print("-" * len(header))

    for kind in ("knowledge", "database", "both", "none"):
        if kind not in by_kind:
            continue
        score = by_kind[kind]
        print(f"{kind:<12}{score.accuracy:>9.0%}{'':>4}{score.total:>4}")

    overall_total = sum(s.total for s in by_kind.values())
    overall_correct = sum(s.correct for s in by_kind.values())
    print("-" * len(header))
    print(f"{'OVERALL':<12}{overall_correct / overall_total:>9.0%}{'':>4}{overall_total:>4}")
    print(f"\nCost: ${total_cost:.4f}")

    for kind, score in by_kind.items():
        if not score.failures:
            continue
        print(f"\n{kind} misses:")
        for question, expected, used in score.failures:
            print(f"  Q: {question}")
            label = "refusal" if kind == "none" else (sorted(expected) or "(none)")
            print(f"     expected {label}, got {sorted(used) or '(none)'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
