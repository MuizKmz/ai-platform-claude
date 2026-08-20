"""connector configuration table

Revision ID: fc6dfa15d184
Revises: fe6983226c3f
Create Date: 2026-08-20 14:17:40.500131

Hand-corrected after autogenerate, which proposed two destructive changes that
are not ours to make:

  - dropping `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, and
    `checkpoint_migrations`. Those belong to LangGraph, not to `Base.metadata`,
    so autogenerate reads them as strays. Applying it would destroy every
    resumable run.

  - replacing the partial index `uq_document_tenant_hash_live` with a full
    unique constraint. The partial index is scoped `WHERE superseded_at IS
    NULL` deliberately: it is what lets a superseded version coexist with the
    version that replaced it. A full constraint would break document
    versioning.

Both were removed. Only the `connector` table is created here.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "fc6dfa15d184"
down_revision: Union[str, Sequence[str], None] = "fe6983226c3f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "connector",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "required_labels",
            postgresql.ARRAY(sa.String()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        # Fernet ciphertext. Never returned by the API in any form.
        sa.Column("credential", sa.Text(), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_error", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_connector_tenant_slug"),
    )
    op.create_index(op.f("ix_connector_tenant_id"), "connector", ["tenant_id"], unique=False)

    # Same policy shape as every other tenant table. A connector row names a
    # host, a database, and a username — reconnaissance material even before
    # the credential is considered.
    op.execute("ALTER TABLE connector ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE connector FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY connector_tenant_isolation ON connector
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON connector TO app_rw")
    # Deliberately NOT granted to app_readonly. That role backs analytics and
    # reporting paths; nothing there needs connector configuration, and a
    # credential column is not something to hand out by default.


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS connector_tenant_isolation ON connector")
    op.execute("ALTER TABLE connector DISABLE ROW LEVEL SECURITY")
    op.drop_index(op.f("ix_connector_tenant_id"), table_name="connector")
    op.drop_table("connector")
