"""pii summary on document

Revision ID: 25b66f8b6125
Revises: fc6dfa15d184
Create Date: 2026-08-20 16:02:11.000000

Hand-corrected after autogenerate, which again proposed dropping LangGraph's
four checkpoint tables. They are not in Base.metadata, so autogenerate reads
them as strays; applying it would destroy every resumable run. Same correction
as fc6dfa15d184 — see that migration's note.

Only the new column is added here.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "25b66f8b6125"
down_revision: Union[str, Sequence[str], None] = "fc6dfa15d184"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Counts by kind, from the ingestion scan: {"email": 4000, "credit_card": 2}.
    # Counts, never values — carrying examples would put the PII into the very
    # metadata written to make it visible.
    #
    # server_default so existing rows read as "{}" rather than NULL. Those rows
    # were ingested before the scan existed and genuinely have no result; the
    # difference between "scanned, found nothing" and "never scanned" is not
    # worth a second column here, and DATA_POLICY.md records that a reindex is
    # what populates them.
    op.add_column(
        "document",
        sa.Column(
            "pii_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("document", "pii_summary")
