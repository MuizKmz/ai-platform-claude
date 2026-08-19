"""Chunk — a slice of a document plus its embedding.

`tenant_id` is denormalised onto this table deliberately. It is derivable by joining
document, but the authorization filter and the RLS policy both need it on the row
being scanned; a join would put the security predicate one table away from the data.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at, uuid_pk

# text-embedding-3-small. Changing model means changing this and re-embedding —
# which is why embedding_model is recorded per row.
EMBEDDING_DIM = 1536


class Chunk(Base):
    __tablename__ = "chunk"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_chunk_document_ordinal"),
        # HNSW for cosine distance. Built here rather than left to a later "performance
        # phase" because the index type constrains the query operator (<=>).
        Index(
            "ix_chunk_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), index=True, nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), index=True, nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    # Recorded per row: a corpus embedded with two different models is unsearchable
    # unless you can tell the rows apart.
    embedding_model: Mapped[str] = mapped_column(nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = created_at()
