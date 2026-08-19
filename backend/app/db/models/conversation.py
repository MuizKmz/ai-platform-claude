"""Conversation and message history, scoped to a tenant like everything else."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at, uuid_pk


class Conversation(Base):
    __tablename__ = "conversation"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(index=True, nullable=False)
    title: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = created_at()


class ConversationMessage(Base):
    """One turn. Cost is recorded per assistant message, not per conversation,
    so a single expensive turn is attributable rather than averaged away."""

    __tablename__ = "conversation_message"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Verified citations only — an unverifiable one never reaches this table.
    citations: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list, nullable=False)
    model: Mapped[str | None] = mapped_column(default=None)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    cost_usd: Mapped[float | None] = mapped_column(Float, default=None)
    refused: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = created_at()
