# ADR 0003 — ARQ for background ingestion

**Status:** Accepted
**Date:** 2026-08-19
**Phase:** 3

## Context

Ingesting the 200-page PDF fixture takes minutes: parse, chunk, then one embedding request
per batch of 64 chunks. Today that happens inside the CLI process, which is tolerable for a
developer and impossible for an API — an HTTP request cannot hold a connection open for
several minutes, and a browser will give up before it finishes.

So ingestion needs to become a job: submitted, tracked, and executed elsewhere.

## Decision

Use **ARQ** for the worker, with Redis as the queue (already running since Phase 0).

Job *state* lives in Postgres, in an `ingest_job` table, not in Redis. ARQ owns the queue;
we own the record.

## Alternatives considered

| Option | Why not |
|---|---|
| **Celery** | The default choice, and much heavier: its own result backend, a large configuration surface, and a threading/prefork model that fits poorly with an async FastAPI app. We would use perhaps 5% of it |
| **Dramatiq** | A reasonable alternative and genuinely simpler than Celery, but sync-first. Our ingestion path is async-friendly and ARQ is built on asyncio |
| **FastAPI `BackgroundTasks`** | Runs in the API process. A restart loses the work with no record it existed, and a long ingestion competes with request handling for the same event loop |
| **A `while True` poller on the jobs table** | No dependency at all, and tempting. But it means writing visibility timeouts, retries, and backoff by hand — a queue, badly |

## Consequences

**Positive:**
- The API returns a job id immediately; nothing blocks on embedding
- Redis was already a dependency, so this adds no new infrastructure
- ARQ is small enough to read: one settings class, functions as tasks

**Negative / accepted costs:**
- A second process to run and supervise in every environment, including CI
- Failures now happen out of band, so job state has to be legible without a stack trace
  in front of you — hence the status, error, and progress columns

## Why job state is in Postgres, not Redis

Redis is a queue here, not a database. Three reasons the record lives in Postgres:

1. **Tenancy.** Every row of tenant data carries `tenant_id` and is protected by RLS. A job
   record naming a tenant's documents is tenant data. Redis has no RLS, so job state there
   would be the one place tenant isolation is enforced by application code alone.
2. **Durability.** A job that vanishes on a Redis restart cannot be resumed, and "did that
   200-page ingest finish?" becomes unanswerable.
3. **Queryability.** "Show me failed jobs for this tenant last week" is a `WHERE` clause
   against a table we already back up.

## How we would remove it

The worker calls `ingest_directory`, which is the same function the CLI calls. Replacing ARQ
means rewriting `app/worker/` — the ingestion logic, the job table, and the API do not move.

The property to preserve in any replacement: **job state stays in Postgres.** A queue that
insists on owning its own state would push tenant data outside RLS, which is a larger change
than swapping a library.
