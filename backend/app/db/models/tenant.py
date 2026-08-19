"""Tenant — the isolation boundary every other table hangs from."""

import uuid
from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at, uuid_pk


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(unique=True, index=True)
    name: Mapped[str]
    created_at: Mapped[datetime] = created_at()
