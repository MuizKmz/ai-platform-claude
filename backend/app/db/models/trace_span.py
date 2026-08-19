"""Trace span — one persisted record per traced operation.

Security invariant 6: every retrieval call emits a trace with a trace ID, latency,
and cost. Stored in our own Postgres rather than a vendor, per the review's
"own your contracts, rent your implementations".
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at, uuid_pk


class TraceSpan(Base):
    __tablename__ = "trace_span"

    id: Mapped[uuid.UUID] = uuid_pk()
    trace_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    span_id: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(nullable=False)
    # Nullable: a 401 produces a span before any tenant is established.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(index=True, default=None)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    token_cost: Mapped[int | None] = mapped_column(Integer, default=None)
    attributes: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = created_at()
