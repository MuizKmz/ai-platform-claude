"""index identifiers with the simple text config

Revision ID: f88c4be5a0fd
Revises: 8b33c58074d2
Create Date: 2026-08-19 16:17:02.426239

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f88c4be5a0fd'
down_revision: Union[str, Sequence[str], None] = '8b33c58074d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    The 'english' config splits "E-402" into 'e' and '-402' at separate
    positions, while websearch_to_tsquery emits them as an adjacency pair — so an
    exact identifier search could never match. Concatenating the 'simple' vector
    keeps identifiers literal while prose keeps its stemming.

    A generated column cannot be altered in place; it is dropped and recreated,
    which recomputes it for every existing row.
    """
    op.drop_index("ix_chunk_search_vector", table_name="chunk")
    op.drop_column("chunk", "search_vector")
    op.add_column(
        "chunk",
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', content) || to_tsvector('simple', content)",
                persisted=True,
            ),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_chunk_search_vector", "chunk", ["search_vector"], postgresql_using="gin"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_chunk_search_vector", table_name="chunk")
    op.drop_column("chunk", "search_vector")
    op.add_column(
        "chunk",
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_chunk_search_vector", "chunk", ["search_vector"], postgresql_using="gin"
    )
