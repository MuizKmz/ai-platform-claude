"""Document management: list and delete.

Ingestion stays out of the API in this stage — a 200-page PDF takes minutes to
embed, and an HTTP request is the wrong place to hold that. Stage 4 moves it to a
worker with a job id.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import CurrentPrincipal, TenantDB
from app.knowledge.lifecycle import delete_document

router = APIRouter(prefix="/v1/documents", tags=["documents"])


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    source_path: str
    labels: list[str]
    version: int
    chunk_count: int
    superseded_at: datetime | None
    created_at: datetime


class DeleteOut(BaseModel):
    documents_deleted: int
    chunks_deleted: int


@router.get("", response_model=list[DocumentOut])
def list_documents(
    principal: CurrentPrincipal,
    db: TenantDB,
    include_superseded: bool = False,
) -> list[DocumentOut]:
    """List documents the caller may see.

    Filtered by the caller's labels, not merely by tenant: a user without the
    `finance` label should not learn that a finance document exists, and a
    listing endpoint that skips the label filter is a quiet way to disclose that.
    """
    rows = db.execute(
        text("""
            SELECT d.id, d.title, d.source_path, d.labels, d.version,
                   d.superseded_at, d.created_at,
                   (SELECT count(*) FROM chunk c WHERE c.document_id = d.id) AS chunk_count
            FROM document d
            WHERE d.tenant_id = :t
              AND d.labels && CAST(:labels AS varchar[])
              AND (:include_superseded OR d.superseded_at IS NULL)
            ORDER BY d.created_at DESC
        """),
        {
            "t": principal.tenant_id,
            "labels": list(principal.allowed_labels),
            "include_superseded": include_superseded,
        },
    ).all()

    return [
        DocumentOut(
            id=row.id,
            title=row.title,
            source_path=row.source_path,
            labels=list(row.labels),
            version=row.version,
            chunk_count=row.chunk_count,
            superseded_at=row.superseded_at,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.delete("/{document_id}", response_model=DeleteOut)
def delete(
    document_id: uuid.UUID,
    principal: CurrentPrincipal,
    db: TenantDB,
) -> DeleteOut:
    """Permanently delete a document and its chunks.

    Requires the `admin` role. Deletion is not reversible and is not a
    tombstone — a caller who can read a document should not thereby be able to
    destroy it for everyone.
    """
    if not principal.has_role("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Deleting a document requires the admin role.",
        )

    # Check visibility under the caller's labels first, so a 404 for a document
    # they may not see is indistinguishable from one that does not exist.
    visible = db.execute(
        text("""
            SELECT 1 FROM document
            WHERE tenant_id = :t AND id = :id AND labels && CAST(:labels AS varchar[])
        """),
        {"t": principal.tenant_id, "id": document_id, "labels": list(principal.allowed_labels)},
    ).scalar()
    if not visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

    result = delete_document(db, tenant_id=principal.tenant_id, document_id=document_id)
    return DeleteOut(
        documents_deleted=result.documents_deleted,
        chunks_deleted=result.chunks_deleted,
    )
