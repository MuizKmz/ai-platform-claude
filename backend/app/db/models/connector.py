"""Connector configuration.

Until now connectors existed only in code — constructed inline in the agent
endpoint from environment variables. That works for two and stops working at
about three, which is the reason this table exists rather than a nicer config
file: connectors are *operated*, and operating them through a redeploy is not
operating them.

**Shape.** The fields every connector has are real columns with real
constraints; the fields that differ by kind live in `settings` as validated
JSONB. A SQL connector needs host/port/database; a REST connector needs a base
URL and a set of endpoints. Modelling that as two tables would mean a third
table and a third API path for a third kind, which is exactly the redesign the
`Connector` ABC exists to avoid.

**The credential is not in `settings`.** It has its own column, holding Fernet
ciphertext, and it is never returned by the API — not to admins, not masked,
not partially. A credential that can be read back is a credential that leaks
through a screenshot, a browser cache, or a support ticket. Write-only is the
whole design, and putting it inside the JSONB blob would have made a careless
`SELECT settings` a disclosure.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at, updated_at, uuid_pk


class ConnectorConfigRecord(Base):
    __tablename__ = "connector"
    __table_args__ = (
        # Slugs are how a connector is named in a tool call and in the audit
        # log, so they must be stable and unambiguous within a tenant. Scoped
        # to the tenant rather than global: two customers may both have an
        # "analytics" connector, and neither should learn of the other.
        UniqueConstraint("tenant_id", "slug", name="uq_connector_tenant_slug"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # 'sql' | 'rest'. Not an enum type: adding a kind should be a migration of
    # code and a row, not an ALTER TYPE that locks the table.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, default=None)

    # Default-deny, as everywhere else. A connector with no labels is reachable
    # by nobody, which is the correct behaviour for one whose configuration was
    # never finished.
    required_labels: Mapped[list[str]] = mapped_column(
        ARRAY(String), default=list, server_default="{}", nullable=False
    )

    # Disabled connectors keep their configuration and stop being offered. The
    # alternative — deleting to disable — loses the audit trail and the
    # credential, and makes "turn it off for an hour" a destructive act.
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )

    # Kind-specific configuration, validated by a Pydantic model per kind
    # before it is ever written. JSONB rather than JSON so it can be indexed
    # and queried if that turns out to be needed.
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )

    # Fernet ciphertext. Never returned by the API in any form.
    credential: Mapped[str | None] = mapped_column(Text, default=None)

    # Result of the last "Test Connection", so an operator can see health
    # without re-probing — the probe is rate-limited precisely because it
    # should not be run casually.
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, default=None)
    # An error CLASS, never upstream error text: a connection failure that
    # echoes the database's message discloses hostnames and versions.
    last_test_error: Mapped[str | None] = mapped_column(String(200), default=None)

    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
