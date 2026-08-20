"""Audit log for connector queries.

Separate from `trace_span` deliberately. A trace answers "why was this slow"; an
audit answers "who read what, and was it allowed". They have different retention
needs, different readers, and — most importantly — different rules about content:
a trace must never carry query text, and an audit is useless without it.

Denied queries are recorded too, and are arguably the more valuable rows. A
sequence of refusals is what an attempted bypass looks like, and a log that
records only successes cannot show it.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at, uuid_pk


class ConnectorAudit(Base):
    __tablename__ = "connector_audit"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Not a foreign key: the audit row must survive the user being deleted.
    # "Who did this" is the whole point, and a cascade would erase it.
    user_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    user_email: Mapped[str] = mapped_column(nullable=False)

    connector_id: Mapped[str] = mapped_column(index=True, nullable=False)

    # The SQL that was actually executed, or the SQL that was refused. Unlike a
    # trace, this table exists to hold it.
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    # The question that produced it, when there was one. Null for direct SQL.
    question: Mapped[str | None] = mapped_column(Text, default=None)

    allowed: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False)
    # Why it was refused. Null when allowed.
    denial_reason: Mapped[str | None] = mapped_column(Text, default=None)

    row_count: Mapped[int | None] = mapped_column(Integer, default=None)
    duration_ms: Mapped[float | None] = mapped_column(Float, default=None)
    # Links an audit row to the trace of the request that produced it.
    trace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)

    created_at: Mapped[datetime] = created_at()
