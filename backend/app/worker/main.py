"""Worker entrypoint.

    uv run arq app.worker.main.WorkerSettings

Runs as its own process. Ingestion must not share an event loop with request
handling: a 200-page PDF would otherwise make every concurrent request wait on
an embedding round-trip.
"""

from __future__ import annotations

from arq.connections import RedisSettings

from app.core.config import settings
from app.worker.tasks import ingest_directory_task


class WorkerSettings:
    """ARQ configuration."""

    functions = [ingest_directory_task]  # noqa: RUF012

    redis_settings = RedisSettings(
        host=settings.redis_host,
        port=settings.redis_port,
    )

    # One job at a time. Ingestion is embedding-bound rather than CPU-bound, and
    # running several concurrently mainly buys provider rate limits.
    max_jobs = 1

    # An hour: a large corpus legitimately takes that long. Too short a timeout
    # kills real work; too long lets a wedged job hold the queue.
    job_timeout = 3600

    # Do NOT retry automatically. Ingestion is idempotent, so a retry is safe —
    # but a job failing for a permanent reason (missing API key, unreadable
    # directory) would retry forever and cost money each time. Resubmitting is a
    # deliberate act.
    max_tries = 1

    # Keep finished jobs briefly for debugging. Durable state is in Postgres.
    keep_result = 3600
