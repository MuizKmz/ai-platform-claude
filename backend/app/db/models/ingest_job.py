"""Ingestion jobs.

State lives here rather than in Redis (ADR 0003): a job record naming a tenant's
documents is tenant data, and Redis has no Row-Level Security.

The columns exist so a failure is legible without a stack trace in front of you.
Background work fails out of band — nobody is watching the terminal — so "which
file, how far in, and what went wrong" has to survive in the row itself.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at, uuid_pk

# Job lifecycle. A job is terminal in "succeeded", "failed", or "cancelled".
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


class IngestJob(Base):
    __tablename__ = "ingest_job"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Who submitted it. Retained so a costly ingestion is attributable.
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    source_path: Mapped[str] = mapped_column(nullable=False)
    labels: Mapped[str] = mapped_column(nullable=False)

    # server_default, not just default: these are written by raw SQL from the API
    # and the worker, which never goes through the ORM. A Python-side default
    # would leave the column NULL and violate the NOT NULL constraint.
    status: Mapped[str] = mapped_column(
        String(16), default="queued", server_default="queued", index=True, nullable=False
    )

    # Progress, updated as the job runs. Without it a long ingestion is
    # indistinguishable from a hung one.
    files_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    files_done: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    documents_created: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    chunks_created: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    # Safe to show a user: parser and provider messages are sanitised before they
    # land here, because they can carry file paths and request content.
    error: Mapped[str | None] = mapped_column(Text, default=None)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = created_at()
