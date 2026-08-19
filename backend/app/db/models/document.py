"""Document — an ingested source file, scoped to one tenant.

`labels` drives authorization. An empty label array means the document is visible
to nobody: default-deny, enforced by the retrieval filter and tested explicitly.
"""

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at, uuid_pk


class Document(Base):
    __tablename__ = "document"
    __table_args__ = (
        # Re-ingesting an unchanged file must not duplicate it. The hash is of
        # content, so a moved or renamed file with identical bytes is still one document.
        # NOTE: the live database carries this as a PARTIAL unique index
        # (uq_document_tenant_hash_live, WHERE superseded_at IS NULL) — see
        # migration 0a96d4380fe4. A partial index cannot be expressed here, so
        # autogenerate will keep proposing to replace it with this plain
        # constraint. Reject that: as a plain constraint it blocks re-ingesting
        # content that was superseded.
        UniqueConstraint("tenant_id", "content_hash", name="uq_document_tenant_hash"),
        # Retrieval filters on superseded_at IS NULL on every query, so it is
        # worth an index rather than a sequential scan over the whole corpus.
        Index("ix_document_live", "tenant_id", "superseded_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str]
    source_path: Mapped[str]
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    labels: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)

    # Versioning. A document is identified across revisions by source_path within
    # a tenant; each new revision increments version and tombstones the old one.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # A tombstone, not a delete. Superseded rows stay so a citation issued last
    # week still resolves to the text that was actually cited — but they are
    # excluded from retrieval, so nobody is answered from a stale revision.
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = created_at()
