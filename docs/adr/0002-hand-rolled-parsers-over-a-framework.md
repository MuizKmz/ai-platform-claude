# ADR 0002 — Format-specific parsers over a document framework

**Status:** Accepted
**Date:** 2026-08-19
**Phase:** 3

## Context

Phase 3 must ingest PDF, DOCX, and HTML. The obvious alternative to writing four small
parsers is adopting a document framework — LlamaIndex readers, Unstructured.io, or
LangChain document loaders — each of which handles all these formats and more.

The review (§E) already argued against LlamaIndex in V1 and left the door open for exactly
this: "adopt it later for its document *readers* if and when you want them, behind your own
interface."

Two things changed the calculation since.

**Table handling is the hard part, and no framework does it the way we need.** The roadmap's
requirement is that a table is not split across chunks. That is not a parsing problem — every
library extracts table text — it is a *chunking* problem, and chunking is ours. A framework
that returns clean text still hands the table to our splitter as an undifferentiated
paragraph, which then breaks it at a row boundary. We need the parser to tell the chunker
"this block is a table, keep it whole", and that is a contract no general loader exposes.

**The dependency cost is not symmetric.** Unstructured.io pulls in a large tree including
optional OCR and ML models. We do not have scanned documents, and the roadmap explicitly
says no OCR until we do.

## Decision

Write a `DocumentParser` protocol returning structured `Block`s (paragraph, heading, table),
with one small implementation per format built on a focused library:

- **PDF** — `pypdf` (pure Python, no system binaries)
- **DOCX** — `python-docx` (the only real option; the format is a zip of XML)
- **HTML** — `beautifulsoup4` + `lxml`
- **Markdown/text** — no library needed

## Alternatives considered

| Option | Why not |
|---|---|
| Unstructured.io | Large dependency tree, optional ML/OCR weight we do not need, and it still does not preserve table boundaries through to our chunker |
| LlamaIndex readers | Would reintroduce a 0.x package with a history of reorganisations, for four files of code |
| LangChain loaders | Same, plus an abstraction layer we would immediately wrap in our own |
| `pdfplumber` for PDF | Better table *detection* than pypdf, but heavier and slower. Worth revisiting if pypdf's heuristics prove insufficient on real documents — the interface makes that a one-file change |

## Consequences

**Positive:**
- Blocks carry their type, so the chunker can keep a table whole — the actual requirement
- Each parser is small enough to read in one sitting and debug when a real document breaks it
- Swapping one format's library touches one file

**Negative / accepted costs:**
- We own the edge cases: multi-column PDFs, headers repeated per page, tables spanning pages
- `pypdf` table detection is heuristic (column alignment), and will be wrong on some real
  PDFs. This is the main risk, and the reason the interface exists
- Four dependencies rather than one

## How we would remove it

`DocumentParser` is the seam. Adopting a framework later means writing one adapter that
returns `Block`s, and deleting four files. The chunker, ingestion, and everything above are
untouched — provided whatever replaces it can still say "this block is a table", which is
the property to check before adopting anything.
