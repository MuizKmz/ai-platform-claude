"""Declarative base and shared column conventions."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base for all ORM models."""


def uuid_pk() -> Mapped[uuid.UUID]:
    """Primary key column. UUIDs, not serial ints.

    A sequential integer key leaks how many rows exist and makes cross-tenant
    ID guessing trivial. Neither is acceptable in a multi-tenant system.
    """
    return mapped_column(primary_key=True, default=uuid.uuid4)


def created_at() -> Mapped[datetime]:
    """Server-side creation timestamp, so it cannot be backdated by the client."""
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
