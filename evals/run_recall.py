"""Retrieval recall against the golden set.

Uses REAL embeddings — this measures semantic quality, which the deterministic
fake cannot do by construction. So it is a script you run deliberately, not a
test CI executes on every push: it costs money and needs a key.

Recall@k here means: for what fraction of questions does the correct section
appear anywhere in the top k? Not precision, not MRR — the question this phase
must answer is "did we retrieve the right thing at all", because if the answer is
no, no amount of generation tuning will save it.

    uv run python ../evals/run_recall.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db.session import owner_engine  # noqa: E402
from app.knowledge.embedding import get_embedding_provider  # noqa: E402
from app.knowledge.ingest import ingest_directory  # noqa: E402
from app.knowledge.retrieval import search  # noqa: E402

EVAL_TENANT = uuid.UUID("6a17aaaa-0000-4000-8000-0000000000e5")
CORPUS = Path(__file__).parent / "corpus"
GOLDEN = Path(__file__).parent / "datasets" / "golden_questions.yaml"
K = 5
TARGET = 0.8


def main() -> int:
    provider = get_embedding_provider()
    questions = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))

    with Session(owner_engine) as session, session.begin():
        # Rebuild the eval corpus from scratch so a run always measures the
        # current chunker and model rather than whatever was ingested last week.
        session.execute(
            text("DELETE FROM chunk WHERE tenant_id = :t"), {"t": EVAL_TENANT}
        )
        session.execute(
            text("DELETE FROM document WHERE tenant_id = :t"), {"t": EVAL_TENANT}
        )
        session.execute(
            text("DELETE FROM tenant WHERE id = :t"), {"t": EVAL_TENANT}
        )
        session.execute(
            text("INSERT INTO tenant (id, slug, name) VALUES (:t, 'eval', 'Eval')"),
            {"t": EVAL_TENANT},
        )
        result = ingest_directory(
            session,
            directory=CORPUS,
            tenant_id=EVAL_TENANT,
            labels=["public"],
            provider=provider,
        )
    print(f"corpus: {result}\n")

    hits = 0
    failures: list[tuple[str, str, list[str]]] = []

    with Session(owner_engine) as session:
        for item in questions:
            results = search(
                session,
                query=item["question"],
                tenant_id=EVAL_TENANT,
                allowed_labels=("public",),
                provider=provider,
                limit=K,
            )
            # The chunker records the heading; a hit means the right section
            # appeared anywhere in the top K.
            retrieved = [r.content[:60] for r in results]
            found = any(
                item["expects"].lower() in r.content.lower()
                or _matches_section(item["expects"], r.content)
                for r in results
            )
            if found:
                hits += 1
            else:
                failures.append((item["question"], item["expects"], retrieved))

    recall = hits / len(questions)
    print(f"Recall@{K}: {recall:.2f}  ({hits}/{len(questions)})")
    print(f"Target:    {TARGET:.2f}")

    if failures:
        print(f"\n{len(failures)} miss(es):")
        for question, expected, retrieved in failures:
            print(f"\n  Q: {question}")
            print(f"  expected section: {expected}")
            for r in retrieved[:3]:
                print(f"    got: {r}...")

    print()
    if recall >= TARGET:
        print(f"PASS - recall {recall:.2f} meets the {TARGET:.2f} baseline")
        return 0
    print(f"FAIL - recall {recall:.2f} is below the {TARGET:.2f} baseline")
    return 1


# Section content keyed by heading, so a hit can be checked without storing the
# heading on the chunk row itself.
_SECTION_MARKERS = {
    "Refund Policy": "refund",
    "Shipping": "shipping",
    "Warranty": "warranty",
    "Data Retention": "retained",
    "Access Control": "minimum access",
    "Password Requirements": "passwords must",
    "Incident Response": "incidents are triaged",
    "Vacation Policy": "paid leave",
    "Expense Claims": "expenses under",
    "Remote Work": "remotely",
}


def _matches_section(expected: str, content: str) -> bool:
    marker = _SECTION_MARKERS.get(expected)
    return bool(marker and marker in content.lower())


if __name__ == "__main__":
    raise SystemExit(main())
