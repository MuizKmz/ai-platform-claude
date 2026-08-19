"""Ingestion: files in, chunks with embeddings out.

Runs under the owner connection. Ingestion is administrative work that writes on
behalf of a tenant rather than acting as one, and it is deliberately not reachable
from the request path.

Idempotent by content hash: re-ingesting an unchanged corpus is a no-op. Without
that, every re-run silently doubles the corpus and quietly wrecks retrieval, since
duplicate chunks crowd out distinct results in the top-k.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.knowledge.chunking import chunk_blocks
from app.knowledge.embedding import EmbeddingProvider
from app.knowledge.parsers import SUPPORTED_SUFFIXES, ParseError, parse_document

logger = logging.getLogger(__name__)

# One request per batch rather than per chunk. Also bounds the failure blast radius:
# a rejected batch loses 64 chunks of work, not a whole corpus.
EMBED_BATCH_SIZE = 64


@dataclass
class IngestResult:
    documents_created: int = 0
    documents_skipped: int = 0
    chunks_created: int = 0

    def __str__(self) -> str:
        return (
            f"{self.documents_created} document(s) ingested, "
            f"{self.documents_skipped} unchanged, "
            f"{self.chunks_created} chunk(s) created"
        )


def ingest_directory(
    session: Session,
    *,
    directory: Path,
    tenant_id: uuid.UUID,
    labels: list[str],
    provider: EmbeddingProvider,
) -> IngestResult:
    """Ingest every supported file under `directory` for one tenant."""
    if not directory.is_dir():
        raise NotADirectoryError(f"not a directory: {directory}")

    result = IngestResult()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            _ingest_file(
                session,
                path=path,
                tenant_id=tenant_id,
                labels=labels,
                provider=provider,
                result=result,
            )
    return result


def _ingest_file(
    session: Session,
    *,
    path: Path,
    tenant_id: uuid.UUID,
    labels: list[str],
    provider: EmbeddingProvider,
    result: IngestResult,
) -> None:
    try:
        parsed = parse_document(path)
    except ParseError:
        # A document we cannot read is skipped rather than aborting the run — one
        # scanned PDF in a folder of 500 should not block the other 499.
        logger.warning("skipping unreadable document: %s", path.name)
        result.documents_skipped += 1
        return

    # Hash the extracted text, not the raw bytes: the same content re-saved by a
    # different tool produces different bytes and the same document.
    content_hash = hashlib.sha256(parsed.text.encode("utf-8")).hexdigest()

    existing = session.execute(
        text("SELECT id FROM document WHERE tenant_id = :t AND content_hash = :h"),
        {"t": tenant_id, "h": content_hash},
    ).scalar()
    if existing:
        result.documents_skipped += 1
        return

    chunks = chunk_blocks(parsed.blocks)
    if not chunks:
        result.documents_skipped += 1
        return

    document_id = session.execute(
        text("""
            INSERT INTO document (id, tenant_id, title, source_path, content_hash, labels)
            VALUES (:id, :t, :title, :src, :hash, :labels)
            RETURNING id
        """),
        {
            "id": uuid.uuid4(),
            "t": tenant_id,
            "title": parsed.title or path.stem,
            "src": str(path),
            "hash": content_hash,
            "labels": labels,
        },
    ).scalar()

    # A heading is prepended to its chunk's embedded text so the section title
    # contributes to the vector — "Refund policy" is often the most retrievable
    # phrase in a section that never repeats those words in its body.
    embed_inputs = [
        f"{chunk.heading}\n\n{chunk.content}" if chunk.heading else chunk.content
        for chunk in chunks
    ]

    vectors: list[list[float]] = []
    for start in range(0, len(embed_inputs), EMBED_BATCH_SIZE):
        vectors.extend(provider.embed(embed_inputs[start : start + EMBED_BATCH_SIZE]))

    for chunk, vector in zip(chunks, vectors, strict=True):
        session.execute(
            text("""
                INSERT INTO chunk
                  (id, tenant_id, document_id, ordinal, content, embedding,
                   embedding_model, embedding_dim)
                VALUES
                  (:id, :t, :doc, :ord, :content, :embedding, :model, :dim)
            """),
            {
                "id": uuid.uuid4(),
                "t": tenant_id,
                "doc": document_id,
                "ord": chunk.ordinal,
                "content": chunk.content,
                "embedding": str(vector),
                "model": provider.model,
                "dim": provider.dimension,
            },
        )

    result.documents_created += 1
    result.chunks_created += len(chunks)
