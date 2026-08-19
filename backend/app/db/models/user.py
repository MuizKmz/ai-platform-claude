"""User — belongs to exactly one tenant.

Roles and labels are stored per user. `allowed_labels` is the permission set that
Phase 1 filters retrieval on; it is server-derived and never accepted from a client.
"""

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at, uuid_pk


class User(Base):
    __tablename__ = "app_user"
    # Email is unique per tenant, not globally: two tenants may both employ
    # alice@example.com and they are different people.
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    email: Mapped[str] = mapped_column(index=True)
    roles: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    allowed_labels: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    created_at: Mapped[datetime] = created_at()
