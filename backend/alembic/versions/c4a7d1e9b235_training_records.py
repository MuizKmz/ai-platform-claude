"""reviewed training records for connected systems

Revision ID: c4a7d1e9b235
Revises: 3b84f9edca86
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4a7d1e9b235"
down_revision: Union[str, Sequence[str], None] = "3b84f9edca86"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "training_record",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.Uuid(), nullable=False),
        sa.Column("system_type", sa.String(length=40), nullable=False),
        sa.Column("environment", sa.String(length=40), nullable=False),
        sa.Column("repository_ref", sa.String(length=500), nullable=True),
        sa.Column("data_classification", sa.String(length=40), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="prompt_ready", nullable=False),
        sa.Column("generated_prompt", sa.Text(), nullable=False),
        sa.Column("submitted_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_email", sa.String(length=320), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connector_id"], ["connector.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('prompt_ready', 'review_required', 'active', 'superseded')",
            name="ck_training_record_status",
        ),
    )
    op.create_index(op.f("ix_training_record_tenant_id"), "training_record", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_training_record_connector_id"), "training_record", ["connector_id"], unique=False)
    op.create_index(op.f("ix_training_record_status"), "training_record", ["status"], unique=False)
    op.execute("ALTER TABLE training_record ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE training_record FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY training_record_tenant_isolation ON training_record
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )
    # Repository-derived system maps can be sensitive.  They are therefore
    # control-plane material, like connectors, and only app_rw can read them.
    op.execute("GRANT SELECT, INSERT, UPDATE ON training_record TO app_rw")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS training_record_tenant_isolation ON training_record")
    op.execute("ALTER TABLE training_record DISABLE ROW LEVEL SECURITY")
    op.drop_index(op.f("ix_training_record_status"), table_name="training_record")
    op.drop_index(op.f("ix_training_record_connector_id"), table_name="training_record")
    op.drop_index(op.f("ix_training_record_tenant_id"), table_name="training_record")
    op.drop_table("training_record")
