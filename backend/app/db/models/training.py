"""Reviewed, repository-derived context for an integration.

Training is deliberately not a model fine-tune.  It is an admin-reviewed map of
business terms and safe capabilities supplied by the system owner.  It carries
no credential and is never activated merely because a repository AI produced
it.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at, updated_at, uuid_pk


class TrainingRecord(Base):
    __tablename__ = "training_record"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    connector_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connector.id", ondelete="CASCADE"), index=True, nullable=False
    )
    system_type: Mapped[str] = mapped_column(String(40), nullable=False)
    environment: Mapped[str] = mapped_column(String(40), nullable=False)
    repository_ref: Mapped[str | None] = mapped_column(String(500), default=None)
    data_classification: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="prompt_ready")
    generated_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON only, validated and secret-scanned before storage.  This is kept
    # separate from the generated prompt so an admin can compare what was asked
    # with what the repository assistant returned.
    submitted_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    reviewed_by_email: Mapped[str | None] = mapped_column(String(320), default=None)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
