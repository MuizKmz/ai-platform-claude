"""Document — an ingested source file, scoped to one tenant.

`labels` drives authorization. An empty label array means the document is visible
to nobody: default-deny, enforced by the retrieval filter and tested explicitly.
"""

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at, uuid_pk


class Document(Base):
    __tablename__ = "document"
    __table_args__ = (
        # Re-ingesting an unchanged file must not duplicate it. The hash is of
        # content, so a moved or renamed file with identical bytes is still one document.
        UniqueConstraint("tenant_id", "content_hash", name="uq_document_tenant_hash"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str]
    source_path: Mapped[str]
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    labels: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    created_at: Mapped[datetime] = created_at()
