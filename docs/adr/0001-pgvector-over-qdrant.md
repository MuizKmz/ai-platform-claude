# ADR 0001 — pgvector over a dedicated vector database

**Status:** Accepted
**Date:** 2026-08-19
**Phase:** 1

## Context

Phase 1 stores chunk embeddings and searches them by cosine similarity. The obvious
alternative is a purpose-built vector database (Qdrant, Weaviate, Milvus, Pinecone).

The decisive constraint is not search performance. It is that **every chunk row must be
filtered by `tenant_id` and by permission labels before ranking** (security invariants 2
and 3). That filter is a join against tenant and document metadata.

If vectors live in a separate store from the metadata, the filter cannot be a `WHERE`
clause. It becomes either a pre-query to fetch permitted IDs and pass them along, or a
post-filter on results — and post-filtering is exactly the "retrieve then hide" pattern
invariant 3 forbids. It also silently breaks top-k: ask for 5, filter 3 away, return 2.

## Decision

Store embeddings in PostgreSQL using pgvector 0.8.6, in the same database as tenant,
document, and chunk metadata. Index with HNSW.

## Alternatives considered

| Option | Why not |
|---|---|
| Qdrant / Weaviate / Milvus | A second datastore to run, back up, and secure. Authorization filtering spans two systems, so the `WHERE` clause becomes application logic — and Row-Level Security cannot backstop it |
| Pinecone | All of the above, plus a network hop, a vendor, and per-query cost during learning |
| FAISS in-process | No persistence, no concurrency, no filtering. Fine for a notebook, not a service |

## Consequences

**Positive:**
- Authorization filter and vector search are one SQL statement, so RLS applies to both
- One datastore: one backup, one connection pool, one set of credentials, one migration tool
- Joins to document metadata are free — titles and labels come back with the results
- Transactional ingestion: a failed ingest rolls back cleanly, leaving no orphan vectors

**Negative / accepted costs:**
- pgvector is slower than a dedicated engine at very large scale (millions of vectors).
  Irrelevant here and for a long time; Postgres handles millions of rows unremarkably
- HNSW index build is memory-hungry and rebuilds are not concurrent by default
- We inherit Postgres connection-pool limits rather than a purpose-built query engine

## How we would remove it

Retrieval sits behind `app/knowledge/retrieval.py`. Swapping stores means reimplementing
that module plus the ingestion writer — the API, the `Principal`, and the tests above it
do not change.

The real cost is not the code: it is that **an external store cannot use RLS**, so tenant
isolation would move from a database guarantee back into application code. Any future move
must replace that guarantee with something equally strong before it ships, not after.
