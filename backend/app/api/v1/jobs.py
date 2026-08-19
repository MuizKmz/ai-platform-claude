"""Ingestion jobs: submit and track.

The API never performs ingestion itself. It writes a job row, enqueues it, and
returns an id — a 200-page PDF takes minutes to embed, and an HTTP request is the
wrong place to hold that.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.api.deps import CurrentPrincipal, TenantDB
from app.core.config import settings

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


class IngestRequest(BaseModel):
    directory: str = Field(min_length=1, max_length=4096)
    labels: list[str] = Field(min_length=1)


class JobOut(BaseModel):
    id: uuid.UUID
    status: str
    source_path: str
    labels: list[str]
    files_total: int
    files_done: int
    documents_created: int
    chunks_created: int
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


@router.post("/ingest", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def submit_ingest(
    request: IngestRequest,
    principal: CurrentPrincipal,
    db: TenantDB,
) -> JobOut:
    """Queue a directory for ingestion.

    Requires the `admin` role: ingestion writes to the corpus everyone else
    reads, and it costs money per run.
    """
    if not principal.has_role("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ingestion requires the admin role.",
        )

    # Labels are required and non-empty by the schema. Restating why: a document
    # with no labels is visible to nobody, so an unlabelled ingest silently
    # produces a corpus nobody can search.
    directory = Path(request.directory)
    if not directory.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Directory not found on the server.",
        )

    job_id = uuid.uuid4()
    db.execute(
        text("""
            INSERT INTO ingest_job
              (id, tenant_id, user_id, source_path, labels, status)
            VALUES (:id, :t, :u, :src, :labels, 'queued')
        """),
        {
            "id": job_id,
            "t": principal.tenant_id,
            "u": principal.user_id,
            "src": str(directory),
            "labels": ",".join(request.labels),
        },
    )

    # Read the row back BEFORE committing.
    #
    # SET LOCAL app.tenant_id is transaction-scoped (that is what stops a pooled
    # connection carrying one request's tenant into the next), so committing here
    # would discard the tenant context and the read that follows would be filtered
    # to zero rows by RLS. The dependency commits when the request completes.
    job = _load(db, principal.tenant_id, job_id)
    db.flush()

    pool = await create_pool(RedisSettings(host=settings.redis_host, port=settings.redis_port))
    try:
        await pool.enqueue_job(
            "ingest_directory_task",
            str(job_id),
            str(principal.tenant_id),
            str(directory),
            request.labels,
        )
    finally:
        await pool.aclose()

    return job


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, principal: CurrentPrincipal, db: TenantDB) -> JobOut:
    """Job status. Scoped by tenant, so another tenant's job id is a 404."""
    return _load(db, principal.tenant_id, job_id)


@router.get("", response_model=list[JobOut])
def list_jobs(principal: CurrentPrincipal, db: TenantDB, limit: int = 20) -> list[JobOut]:
    rows = db.execute(
        text("""
            SELECT * FROM ingest_job
            WHERE tenant_id = :t
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {"t": principal.tenant_id, "limit": max(1, min(limit, 100))},
    ).all()
    return [_to_out(row) for row in rows]


def _load(db: TenantDB, tenant_id: uuid.UUID, job_id: uuid.UUID) -> JobOut:
    row = db.execute(
        text("SELECT * FROM ingest_job WHERE tenant_id = :t AND id = :id"),
        {"t": tenant_id, "id": job_id},
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return _to_out(row)


def _to_out(row: object) -> JobOut:
    return JobOut(
        id=row.id,  # type: ignore[attr-defined]
        status=row.status,  # type: ignore[attr-defined]
        source_path=row.source_path,  # type: ignore[attr-defined]
        labels=[x for x in row.labels.split(",") if x],  # type: ignore[attr-defined]
        files_total=row.files_total,  # type: ignore[attr-defined]
        files_done=row.files_done,  # type: ignore[attr-defined]
        documents_created=row.documents_created,  # type: ignore[attr-defined]
        chunks_created=row.chunks_created,  # type: ignore[attr-defined]
        error=row.error,  # type: ignore[attr-defined]
        started_at=row.started_at,  # type: ignore[attr-defined]
        finished_at=row.finished_at,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
    )
