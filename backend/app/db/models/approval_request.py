"""Proposed write actions, awaiting a human.

This table is the phase. Everything before it was read-only, which capped the
blast radius of every bug at "wrong answer". A write changes that to "wrong
action in a production system", so the mechanism that stands between the two is
worth more care than the writing itself.

**A row here is a proposal, not an instruction.** The agent creates it and
stops. Nothing in the agent, the tool registry, or any request the agent can
make is able to execute it. Execution happens in one function, called from one
endpoint, which refuses unless this row says a named human approved it.

**The payload is stored exactly as it will be sent.** Not a summary, not a
description — the literal request body. A human approving "create a ticket for
the shipping delay" without seeing the payload is approving a sentence, and the
sentence is not what gets sent. Dry-run shows this same field, which is what
makes the preview honest.

**Terminal states are terminal.** approved → executed, or rejected, or expired.
A rejected request cannot be revived, and an executed one cannot be executed
again; both are enforced by the executor rather than by convention, because a
proposal that can be replayed is a duplicate write waiting to happen.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at, uuid_pk


class ApprovalStatus:
    """Not a Python enum, and not a Postgres one.

    A plain string with a check constraint: adding a state later should be a
    migration and a constant, not an ALTER TYPE that locks the table.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    EXPIRED = "expired"

    #: States from which execution is possible. Deliberately one.
    EXECUTABLE = (APPROVED,)

    #: States that can never change again.
    TERMINAL = (REJECTED, EXECUTED, FAILED, EXPIRED)


class ApprovalRequest(Base):
    __tablename__ = "approval_request"
    __table_args__ = (
        # The idempotency key is what stops one approved proposal producing two
        # writes — a double-click, a retried request, a worker that restarted
        # mid-execution. Unique per tenant rather than globally: two tenants
        # generating the same key is a coincidence, not a conflict.
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_approval_tenant_idem"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(16),
        default=ApprovalStatus.PENDING,
        server_default="pending",
        nullable=False,
        index=True,
    )

    # --- what is proposed -----------------------------------------------------

    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    connector_slug: Mapped[str] = mapped_column(String(64), nullable=False)

    # A human-readable summary, for the queue listing. Never the basis of a
    # decision — `payload` is.
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    # The exact request that will be sent, byte for byte. What dry-run shows,
    # and what an approver is actually approving.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Where it will be sent. Stored alongside the payload because "create a
    # ticket" means something different against a staging host.
    target_method: Mapped[str] = mapped_column(String(8), nullable=False)
    target_path: Mapped[str] = mapped_column(String(500), nullable=False)

    # Sent as the Idempotency-Key header, and unique per tenant above. Generated
    # when the proposal is created, not when it is approved, so an approval
    # replayed twice carries the same key and the upstream deduplicates.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- who proposed it ------------------------------------------------------

    requested_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    requested_by_email: Mapped[str] = mapped_column(String(320), nullable=False)
    # The question that led here, so an approver can see the intent behind the
    # payload rather than only the payload.
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(default=None, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)

    # --- who decided ----------------------------------------------------------
    # Null until a human acts. The DoD requires the audit log to reconstruct who
    # approved what, when, and why — these four columns are that record.

    decided_by: Mapped[uuid.UUID | None] = mapped_column(default=None)
    decided_by_email: Mapped[str | None] = mapped_column(String(320), default=None)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    decision_note: Mapped[str | None] = mapped_column(Text, default=None)

    # --- what happened --------------------------------------------------------

    executed_at: Mapped[datetime | None] = mapped_column(default=None)
    response_status: Mapped[int | None] = mapped_column(default=None)
    # An error CLASS, never upstream text — the same rule as connector probes.
    error: Mapped[str | None] = mapped_column(String(500), default=None)

    # Set when a compensating action undoes this write. Recorded rather than
    # deleting the row: "this was done and then undone" and "this never
    # happened" are different facts.
    compensated_at: Mapped[datetime | None] = mapped_column(default=None)
    compensated_by: Mapped[uuid.UUID | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = created_at()

    # Proposals go stale. An approval queue nobody clears becomes a list of
    # actions whose context has moved on, and approving one of those a week
    # later is worse than losing it.
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
