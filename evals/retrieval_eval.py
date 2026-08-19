"""Retrieval scorecard: vector-only versus hybrid, on the golden set.

Uses REAL embeddings, because the deterministic fake cannot measure semantic
quality by construction. So this is a script you run deliberately — it costs a
few cents — rather than a test CI runs on every push.

    uv run python ../evals/retrieval_eval.py
    uv run python ../evals/retrieval_eval.py --chunk-size 600 --overlap 80

Metrics:

  Recall@k  For what fraction of answerable questions does the correct section
            appear anywhere in the top k? The question this phase must answer is
            "did we retrieve the right thing at all" — if the answer is no, no
            amount of generation tuning helps.

  MRR       Mean reciprocal rank. Recall says the right chunk was somewhere in
            the top 5; MRR says whether it was 1st or 5th. That matters because
            the generator reads the top chunks first and its context is finite.

  Noise     On unanswerable questions, the fraction of runs that returned any
            result at all. Not a failure — retrieval cannot know the corpus is
            silent — but a rising number means the top-k is filling with
            irrelevant text the generator then has to refuse.

Results are reported per category. An aggregate hides exactly the trade-off
hybrid search exists to fix: dense embeddings win on paraphrase and lose on
identifiers, and one number averages the two into invisibility.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db.session import owner_engine  # noqa: E402
from app.knowledge.chunking import chunk_blocks  # noqa: E402
from app.knowledge.embedding import get_embedding_provider  # noqa: E402
from app.knowledge.hybrid import hybrid_search  # noqa: E402
from app.knowledge.parsers import parse_document  # noqa: E402
from app.knowledge.retrieval import search  # noqa: E402

EVAL_TENANT = uuid.UUID("6a17aaaa-0000-4000-8000-0000000000e5")
CORPUS = Path(__file__).parent / "corpus"
GOLDEN = Path(__file__).parent / "datasets" / "golden_questions.yaml"
K = 5
# Recall@5 saturates on any corpus small enough to iterate on quickly: with a
# few dozen chunks the right one is nearly always somewhere in the top five.
# Recall@1 does not saturate, and it is the number that actually matters — the
# generator reads the top chunk first and its context window is finite.
REPORT_KS = (1, 3, 5)

# Content that identifies each section, so a hit can be checked without storing
# the heading on the chunk row itself.
SECTION_MARKERS = {
    "Refund Policy": "refund",
    "Shipping": "shipping",
    "Warranty": "warranty",
    "Data Retention": "retained for seven years",
    "Access Control": "minimum access",
    "Password Requirements": "passwords must",
    "Incident Response": "incidents are triaged",
    "Vacation Policy": "paid leave",
    "Expense Claims": "expenses under",
    "Remote Work": "remotely",
    "Onboarding": "new starters",
    "Procurement": "purchase orders",
    "Support Hours": "standard support",
    "Escalation": "p1 incident",
    "Backup Schedule": "full backups",
    "Change Management": "change request",
    "Travel Policy": "economy class",
    "Parts Catalogue": "part number",
    "Error Codes": "e-402",
}


@dataclass
class CategoryScore:
    total: int = 0
    hits: int = 0
    ranks: list[int] = field(default_factory=list)
    reciprocal_ranks: list[float] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.hits / self.total if self.total else 0.0

    @property
    def mrr(self) -> float:
        return sum(self.reciprocal_ranks) / self.total if self.total else 0.0

    def recall_at(self, k: int) -> float:
        if not self.total:
            return 0.0
        return sum(1 for rank in self.ranks if rank <= k) / self.total


def _matches(expected: str, content: str) -> bool:
    marker = SECTION_MARKERS.get(expected, expected.lower())
    return marker in content.lower()


def _rebuild_corpus(chunk_size: int, overlap: int) -> int:
    """Re-ingest the eval corpus from scratch.

    Rebuilt every run so a scorecard always measures the current chunker and
    model rather than whatever happened to be ingested last week.
    """
    provider = get_embedding_provider()
    chunks_written = 0

    with Session(owner_engine) as session, session.begin():
        for table in ("chunk", "document"):
            session.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :t"),  # noqa: S608
                {"t": EVAL_TENANT},
            )
        session.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": EVAL_TENANT})
        session.execute(
            text("INSERT INTO tenant (id, slug, name) VALUES (:t, 'eval', 'Eval')"),
            {"t": EVAL_TENANT},
        )

        for path in sorted(CORPUS.glob("*.md")):
            parsed = parse_document(path)
            chunks = chunk_blocks(parsed.blocks, chunk_size=chunk_size, overlap=overlap)
            document_id = uuid.uuid4()
            session.execute(
                text("""
                    INSERT INTO document
                      (id, tenant_id, title, source_path, content_hash, labels)
                    VALUES (:id, :t, :title, :src, :hash, '{public}')
                """),
                {
                    "id": document_id,
                    "t": EVAL_TENANT,
                    "title": parsed.title or path.stem,
                    "src": str(path),
                    "hash": uuid.uuid4().hex,
                },
            )
            vectors = provider.embed([c.content for c in chunks])
            for chunk, vector in zip(chunks, vectors, strict=True):
                session.execute(
                    text("""
                        INSERT INTO chunk
                          (id, tenant_id, document_id, ordinal, content, embedding,
                           embedding_model, embedding_dim)
                        VALUES (:id, :t, :d, :o, :content, :emb, :model, :dim)
                    """),
                    {
                        "id": uuid.uuid4(),
                        "t": EVAL_TENANT,
                        "d": document_id,
                        "o": chunk.ordinal,
                        "content": chunk.content,
                        "emb": str(vector),
                        "model": provider.model,
                        "dim": provider.dimension,
                    },
                )
            chunks_written += len(chunks)

    return chunks_written


def _evaluate(strategy: str, questions: list[dict]) -> dict[str, CategoryScore]:
    provider = get_embedding_provider()
    scores: dict[str, CategoryScore] = {}

    with Session(owner_engine) as session:
        for item in questions:
            kind = item["kind"]
            score = scores.setdefault(kind, CategoryScore())
            score.total += 1

            if strategy == "vector":
                results = [
                    hit.content
                    for hit in search(
                        session,
                        query=item["question"],
                        tenant_id=EVAL_TENANT,
                        allowed_labels=("public",),
                        provider=provider,
                        limit=K,
                    )
                ]
            else:
                results = [
                    fused.hit.content
                    for fused in hybrid_search(
                        session,
                        query=item["question"],
                        tenant_id=EVAL_TENANT,
                        allowed_labels=("public",),
                        provider=provider,
                        limit=K,
                    )
                ]

            if kind == "unanswerable":
                # "hits" here counts noise: results returned when none should be.
                if results:
                    score.hits += 1
                continue

            rank = next(
                (i for i, content in enumerate(results, start=1) if _matches(item["expects"], content)),
                None,
            )
            if rank is not None:
                score.hits += 1
                score.ranks.append(rank)
                score.reciprocal_ranks.append(1.0 / rank)
            else:
                score.misses.append(item["question"])

    return scores


def _print_scorecard(label: str, scores: dict[str, CategoryScore]) -> None:
    print(f"\n{label}")
    print("-" * len(label))
    header = "  " + " " * 12 + "".join(f"R@{k}    " for k in REPORT_KS) + "MRR"
    print(header)
    for kind in ("semantic", "exact"):
        if kind in scores:
            s = scores[kind]
            cells = "".join(f"{s.recall_at(k):.2f}  " for k in REPORT_KS)
            print(f"  {kind:12}{cells}{s.mrr:.3f}")

    answerable = [s for kind, s in scores.items() if kind != "unanswerable"]
    total = sum(s.total for s in answerable)
    all_ranks = [rank for s in answerable for rank in s.ranks]
    cells = "".join(
        f"{sum(1 for r in all_ranks if r <= k) / total:.2f}  " for k in REPORT_KS
    )
    mrr = sum(sum(s.reciprocal_ranks) for s in answerable) / total if total else 0.0
    print(f"  {'OVERALL':12}{cells}{mrr:.3f}")

    if "unanswerable" in scores:
        s = scores["unanswerable"]
        print(f"  {'noise':12} {s.hits}/{s.total} unanswerable questions returned results")


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrieval scorecard")
    parser.add_argument("--chunk-size", type=int, default=1200)
    parser.add_argument("--overlap", type=int, default=150)
    parser.add_argument("--skip-rebuild", action="store_true")
    args = parser.parse_args()

    questions = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))

    if not args.skip_rebuild:
        chunks = _rebuild_corpus(args.chunk_size, args.overlap)
        print(f"corpus rebuilt: {chunks} chunks (size={args.chunk_size}, overlap={args.overlap})")

    vector_scores = _evaluate("vector", questions)
    hybrid_scores = _evaluate("hybrid", questions)

    _print_scorecard("VECTOR ONLY", vector_scores)
    _print_scorecard("HYBRID (RRF)", hybrid_scores)

    def overall(scores: dict[str, CategoryScore]) -> float:
        answerable = [s for kind, s in scores.items() if kind != "unanswerable"]
        total = sum(s.total for s in answerable)
        return sum(s.hits for s in answerable) / total if total else 0.0

    delta = overall(hybrid_scores) - overall(vector_scores)
    print(f"\nHybrid vs vector: {delta:+.2f} Recall@{K}")

    misses = hybrid_scores.get("exact", CategoryScore()).misses
    if misses:
        print(f"\nHybrid still misses {len(misses)} exact-term question(s):")
        for question in misses:
            print(f"  - {question}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
