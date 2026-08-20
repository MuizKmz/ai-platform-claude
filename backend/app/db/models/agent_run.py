"""Agent runs.

Exists because LangGraph's checkpoint tables carry no tenant_id and therefore
sit outside Row-Level Security. This table does carry one, has a policy, and is
consulted before any checkpoint is touched — so resuming a run passes a tenant
check the database enforces, even though the checkpoint itself is not protected.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at, uuid_pk


class AgentRunRecord(Base):
    __tablename__ = "agent_run"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)

    # The LangGraph thread. Server-generated and unique, so it cannot be
    # guessed into collision with another tenant's run.
    thread_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, default=None)
    halted_reason: Mapped[str | None] = mapped_column(Text, default=None)

    steps: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    tool_call_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # Counted separately from failures: a run of denials is what an escalation
    # attempt looks like, and it is invisible among ordinary tool errors.
    denied_tool_calls: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, server_default="0", nullable=False)

    routed_directly: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    trace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)

    created_at: Mapped[datetime] = created_at()
