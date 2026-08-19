# Experiment — chunking configuration comparison

**Date:** 2026-08-19 · **Phase:** 3 · **Status:** null result, and the reason matters

## Question

The roadmap names "should chunks be 500 or 1000 tokens?" as the question you will spend the
most time on, and requires at least three configurations compared **by number**.

## Setup

- Corpus: `evals/corpus/` — 20 policy sections plus 30 distractor sections
- Golden set: 55 questions (34 semantic, 10 exact, 10 unanswerable)
- Embeddings: `openai/text-embedding-3-small`
- Retrieval: vector-only (see [hybrid-search-comparison](hybrid-search-comparison.md))

## Result

| chunk_size | overlap | chunks | semantic R@1 | exact R@1 | overall R@1 | MRR |
|---|---|---|---|---|---|---|
| 400 | 60 | 51 | 0.88 | 1.00 | 0.91 | 0.947 |
| 800 | 100 | 50 | 0.88 | 1.00 | 0.91 | 0.947 |
| 1600 | 200 | 50 | 0.88 | 1.00 | 0.91 | 0.947 |

**Identical to three decimal places.** Not "close" — the same chunks were produced each time.

## Why

The corpus is structured as short sections under headings:

```
chunks: 50   avg length: 264 chars   max: 407 chars   over 400 chars: 1
```

Every section is already smaller than the smallest budget tested, so `_split_section` never
splits anything. Chunk boundaries fall on headings, and the size parameter is inert.

That is not an artefact of a toy corpus — it is what structure-aware chunking is *for*. The
splitter prefers to break where the author already broke, and a well-headed document gives
it enough boundaries that a length budget rarely applies. Chunk size only becomes the
dominant variable on documents without that structure: long unbroken prose, transcripts,
scanned reports, minutes.

## Decision

**Keep the defaults** (`chunk_size=1200`, `overlap=150`). They are as good as any other
value here, and 1200 characters (~300 tokens) leaves room for several chunks in a
generation context window without crowding out the top-ranked result.

The honest summary: **this experiment did not measure what it set out to measure.** It
measured that our corpus does not exercise the parameter. Reporting 0.91 three times as
though three options had been weighed would have been the misleading version.

## What would make this experiment informative

1. **A corpus with long unstructured documents** — a transcript, a contract, minutes. That
   is where the splitter actually fires and where the parameter has consequences.
2. **The 200-page PDF fixture** (`backend/tests/fixtures/documents/handbook.pdf`), which
   has real page-level structure and no headings within pages.
3. **Measuring cost as well as recall.** Smaller chunks mean more of them, which means more
   embedding calls at ingestion and more rows scanned per query. Recall alone cannot
   justify a chunk size; recall per dollar can.

Re-run with:

```powershell
cd backend
uv run python ../evals/retrieval_eval.py --chunk-size 400 --overlap 60
```
