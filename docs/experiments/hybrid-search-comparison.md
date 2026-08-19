# Experiment — hybrid search vs vector-only

**Date:** 2026-08-19 · **Phase:** 3 · **Status:** hybrid built, **disabled by default**

## Question

The roadmap asserts hybrid search is "the single highest-value retrieval improvement
available", because enterprise text is full of part numbers and error codes that dense
embeddings handle badly. Does it beat the vector-only baseline **on this corpus**?

## Setup

- Corpus: `evals/corpus/` — 20 policy sections plus 30 distractor sections, 51 chunks
  at `chunk_size=400, overlap=60`
- Golden set: 55 questions — 34 semantic (paraphrased, sharing no words with the answer),
  10 exact (part numbers and error codes), 10 unanswerable
- Embeddings: `openai/text-embedding-3-small`
- Fusion: Reciprocal Rank Fusion, k=60, candidate depth 30 per retriever

Recall@5 saturates at 1.00 on a corpus this size, so **Recall@1 is the discriminating
metric** — and it is the one that matters anyway, since the generator reads the top chunk
first and its context window is finite.

## Result

| keyword weight | semantic R@1 | exact R@1 | overall R@1 | MRR |
|---|---|---|---|---|
| **0.0 (vector only)** | **0.88** | **1.00** | **0.91** | **0.947** |
| 0.05 | 0.79 | 1.00 | 0.84 | 0.900 |
| 0.1 | 0.74 | 1.00 | 0.80 | 0.852 |
| 0.2 | 0.74 | 1.00 | 0.80 | 0.830 |
| 0.3 | 0.65 | 1.00 | 0.73 | 0.795 |
| 0.5 | 0.65 | 1.00 | 0.73 | 0.799 |
| 1.0 (equal weight) | 0.59 | 1.00 | 0.68 | 0.758 |

**Hybrid search made retrieval monotonically worse.** Not marginally, and not only at high
weights: even a 5% keyword contribution cost 9 points of semantic Recall@1.

## Why

**The keyword retriever must use OR semantics, and OR is noisy.** `websearch_to_tsquery`
joins terms with AND, so "What does error E-402 mean?" demands a chunk containing *what*,
*does*, *error*, *E-402* and *mean* — the error-codes table contains none of the question
words, so AND matched nothing at all. Switching to OR made the retriever functional and
simultaneously made it noisy: any chunk sharing one common word becomes a candidate, and
enough of that noise ranked highly to displace correct answers.

**The exact-term case that motivates hybrid does not arise here.** Exact R@1 is 1.00 for
both strategies, so hybrid has nothing to rescue. The reason is structural: our chunker
keeps a table whole, so every part number lives in a single chunk that also contains the
words "Part Number", "Unit Price", "Lead Time". A question about *any* identifier retrieves
that chunk on semantics alone. Table-aware chunking (Phase 3 stage 1) removed the very
failure that hybrid search was meant to address.

**The corpus is small and topically distinct.** 51 chunks across unrelated subjects is an
easy retrieval problem. These numbers should not be generalised to a corpus of 50,000
chunks with overlapping terminology, where lexical matching plausibly earns its place.

## Decision

**Hybrid search stays in the codebase, disabled by default** (`HYBRID_SEARCH_ENABLED=false`).

Building it was not wasted: it is tested, it works, and the eval harness can now answer this
question again in one command whenever the corpus changes. Enabling it is a config flag, not
a code change.

Shipping it on this evidence would have meant shipping a measured 23-point regression in
overall Recall@1 because a plan said it was valuable. The roadmap's own rule is that hybrid
must "beat the Phase 1 baseline by a number recorded in the experiment log" — the number is
recorded, and it is negative.

## What would change this

Re-run `uv run python ../evals/retrieval_eval.py` when any of these becomes true:

1. **The corpus grows past a few thousand chunks with overlapping vocabulary.** Dense
   retrieval degrades as near-duplicates accumulate; lexical matching does not.
2. **Identifiers stop sharing a chunk with their labels** — long tables split across
   chunks, or identifiers scattered through prose.
3. **Users search for rare literal strings** — a specific ticket id, a customer name, an
   error string copied from a log. None of the 55 golden questions do this, which is a gap
   in the golden set as much as a finding about hybrid search.

A better next experiment than tuning the weight: **route by query shape.** Use keyword
retrieval only when the query contains an identifier-like token (`[A-Z]{1,3}-\d{3,}`),
rather than blending on every query. That targets the case hybrid is good at without paying
its noise cost on natural-language questions.
