"""Background ingestion tasks.

Resumability is the property worth explaining. A job killed halfway does not
resume from a checkpoint it wrote — it simply runs again, and every file it
already ingested is skipped by content hash. Idempotent ingestion (Phase 1) is
what makes "resume" and "retry" the same operation, and it means there is no
checkpoint to keep in step with reality.

Each file is committed on its own. A single transaction around a 500-file
directory would mean one unreadable file at file 499 discards 498 files of paid
embedding work.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import owner_engine
from app.knowledge.embedding import EmbeddingError, get_embedding_provider
from app.knowledge.ingest import IngestResult, ingest_file
from app.knowledge.parsers import SUPPORTED_SUFFIXES

logger = logging.getLogger(__name__)


def _wait_for_job_row(job_id: uuid.UUID, attempts: int = 10, delay: float = 0.5) -> bool:
    """Poll for the job row, which the submitting request may still be committing."""
    for _ in range(attempts):
        with Session(owner_engine) as session:
            found = session.execute(
                text("SELECT 1 FROM ingest_job WHERE id = :id"), {"id": job_id}
            ).scalar()
        if found:
            return True
        time.sleep(delay)
    return False


def _set_status(job_id: uuid.UUID, **fields: Any) -> None:
    """Update a job row in its own transaction.

    Separate from the ingestion transaction on purpose: progress must be visible
    while the work is still running, and a status write that rolls back with a
    failed file would leave the job looking stuck.
    """
    assignments = ", ".join(f"{key} = :{key}" for key in fields)
    with Session(owner_engine) as session, session.begin():
        session.execute(
            text(f"UPDATE ingest_job SET {assignments} WHERE id = :id"),  # noqa: S608
            {**fields, "id": job_id},
        )


async def ingest_directory_task(
    ctx: dict[str, Any],
    job_id: str,
    tenant_id: str,
    directory: str,
    labels: list[str],
) -> dict[str, int]:
    """Ingest a directory for one tenant, reporting progress as it goes."""
    job_uuid = uuid.UUID(job_id)
    tenant_uuid = uuid.UUID(tenant_id)
    root = Path(directory)

    # The API enqueues inside the request transaction, so the job row may not be
    # committed yet when the worker picks it up. Waiting briefly is simpler and
    # safer than enqueueing after commit, where a crash between the two would
    # leave a job row nobody ever runs.
    if not _wait_for_job_row(job_uuid):
        logger.error("job %s never appeared; the submitting transaction rolled back", job_id)
        return {"documents_created": 0, "documents_skipped": 0, "chunks_created": 0}

    try:
        provider = get_embedding_provider()
    except EmbeddingError as exc:
        # The message names the missing setting, not a key value.
        _set_status(
            job_uuid,
            status="failed",
            error=str(exc),
            finished_at=datetime.now(UTC),
        )
        raise

    files = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    ]

    _set_status(
        job_uuid,
        status="running",
        started_at=datetime.now(UTC),
        files_total=len(files),
    )

    result = IngestResult()
    try:
        for index, path in enumerate(files, start=1):
            # One transaction per file: an unreadable file at 499 of 500 must not
            # discard the 498 that already cost money to embed.
            with Session(owner_engine) as session, session.begin():
                ingest_file(
                    session,
                    path=path,
                    tenant_id=tenant_uuid,
                    labels=labels,
                    provider=provider,
                    result=result,
                )
            _set_status(
                job_uuid,
                files_done=index,
                documents_created=result.documents_created,
                chunks_created=result.chunks_created,
            )
    except Exception as exc:
        logger.exception("ingest job %s failed", job_id)
        _set_status(
            job_uuid,
            status="failed",
            # Deliberately generic. Provider and parser messages can carry file
            # paths and request content, and this field is shown to a caller.
            error=f"{type(exc).__name__} while ingesting",
            finished_at=datetime.now(UTC),
        )
        raise

    _set_status(
        job_uuid,
        status="succeeded",
        finished_at=datetime.now(UTC),
        documents_created=result.documents_created,
        chunks_created=result.chunks_created,
    )
    return {
        "documents_created": result.documents_created,
        "documents_skipped": result.documents_skipped,
        "chunks_created": result.chunks_created,
    }
