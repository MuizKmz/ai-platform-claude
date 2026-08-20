"""approval requests

Revision ID: 2e3ebe7f926a
Revises: 25b66f8b6125
Create Date: 2026-08-20 17:14:02.000000

Hand-corrected after autogenerate, which again proposed dropping LangGraph's
four checkpoint tables — they are not in Base.metadata, so it reads them as
strays. Same correction as fc6dfa15d184 and 25b66f8b6125.

Two things added here that autogenerate cannot know about: the RLS policy, and
a CHECK constraint pinning the status vocabulary. The check matters more than it
looks — the executor refuses to run anything not in `approved`, and a typo'd
status would silently make a row unexecutable rather than loudly invalid.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "2e3ebe7f926a"
down_revision: Union[str, Sequence[str], None] = "25b66f8b6125"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "approval_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        # What is proposed.
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("connector_slug", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        # The exact request body. What an approver actually approves.
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("target_method", sa.String(length=8), nullable=False),
        sa.Column("target_path", sa.String(length=500), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        # Who proposed it.
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("requested_by_email", sa.String(length=320), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        # Who decided. Null until a human acts.
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_by_email", sa.String(length=320), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        # What happened.
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("compensated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("compensated_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One approved proposal, one write. A double-click, a retried request,
        # or a worker restarting mid-execution reuses the key rather than
        # creating a second action.
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_approval_tenant_idem"),
        # The status vocabulary, pinned. A typo would otherwise produce a row
        # that is silently unexecutable rather than loudly invalid.
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'executed', 'failed', 'expired')",
            name="ck_approval_status",
        ),
        # An approved row must name who approved it. This is the DoD's
        # "recorded human approval", expressed as a constraint rather than as
        # a convention — the executor checks it too, and a control worth having
        # is worth having twice.
        sa.CheckConstraint(
            "status <> 'approved' OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_approval_has_approver",
        ),
    )
    op.create_index(
        op.f("ix_approval_request_tenant_id"), "approval_request", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_approval_request_status"), "approval_request", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_approval_request_agent_run_id"),
        "approval_request",
        ["agent_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_approval_request_trace_id"), "approval_request", ["trace_id"], unique=False
    )

    # Same policy shape as every other tenant table. A proposal names a target
    # system, a path, and a payload — one tenant reading another's queue would
    # learn what systems they run and what their staff are doing in them.
    op.execute("ALTER TABLE approval_request ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE approval_request FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY approval_request_tenant_isolation ON approval_request
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON approval_request TO app_rw")
    # No DELETE grant, deliberately. An approval record is the audit trail for a
    # write that reached a production system; deleting one removes the evidence
    # that it happened. Retention expires them by status, never by removal.


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS approval_request_tenant_isolation ON approval_request")
    op.execute("ALTER TABLE approval_request DISABLE ROW LEVEL SECURITY")
    op.drop_index(op.f("ix_approval_request_trace_id"), table_name="approval_request")
    op.drop_index(op.f("ix_approval_request_agent_run_id"), table_name="approval_request")
    op.drop_index(op.f("ix_approval_request_status"), table_name="approval_request")
    op.drop_index(op.f("ix_approval_request_tenant_id"), table_name="approval_request")
    op.drop_table("approval_request")
