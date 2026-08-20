"""row level security on trace_span

Revision ID: 24e06b6f5a57
Revises: 765a221fcf5a
Create Date: 2026-08-20 09:51:41.835828

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '24e06b6f5a57'
down_revision: Union[str, Sequence[str], None] = '765a221fcf5a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    trace_span was the one tenant-scoped table without a policy — it carries
    tenant_id but never enforced it, so any session could read every tenant's
    spans. Found by auditing pg_policies against pg_tables rather than by a
    failing test, which is why the test below now exists too.

    A span records which tenant made a request, how long it took, and how much it
    cost. That is tenant data, and it is about to be exposed through a trace
    viewer in Phase 6.

    tenant_id is nullable here (a 401 produces a span before any tenant is
    established), so the policy admits NULL rows as well. They belong to no
    tenant and disclose nothing about one.
    """
    op.execute("ALTER TABLE trace_span ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE trace_span FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY trace_span_tenant_isolation ON trace_span
        USING (
            tenant_id IS NULL
            OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        )
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP POLICY IF EXISTS trace_span_tenant_isolation ON trace_span")
    op.execute("ALTER TABLE trace_span DISABLE ROW LEVEL SECURITY")
