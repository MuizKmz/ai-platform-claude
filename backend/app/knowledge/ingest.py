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
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.pii import summarise
from app.knowledge.chunking import chunk_blocks
from app.knowledge.embedding import EmbeddingProvider
from app.knowledge.lifecycle import find_live_by_source, supersede_document
from app.knowledge.parsers import SUPPORTED_SUFFIXES, ParseError, parse_document

logger = logging.getLogger(__name__)

# One request per batch rather than per chunk. Also bounds the failure blast radius:
# a rejected batch loses 64 chunks of work, not a whole corpus.
EMBED_BATCH_SIZE = 64


@dataclass
class IngestResult:
    documents_created: int = 0
    documents_skipped: int = 0
    documents_superseded: int = 0
    chunks_created: int = 0

    def __str__(self) -> str:
        parts = [
            f"{self.documents_created} document(s) ingested",
            f"{self.documents_skipped} unchanged",
        ]
        if self.documents_superseded:
            parts.append(f"{self.documents_superseded} superseded")
        parts.append(f"{self.chunks_created} chunk(s) created")
        return ", ".join(parts)


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
            ingest_file(
                session,
                path=path,
                tenant_id=tenant_id,
                labels=labels,
                provider=provider,
                result=result,
            )
    return result


def ingest_file(
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

    # Identical content already live: nothing to do.
    existing = session.execute(
        text("""
            SELECT id FROM document
            WHERE tenant_id = :t AND content_hash = :h AND superseded_at IS NULL
        """),
        {"t": tenant_id, "h": content_hash},
    ).scalar()
    if existing:
        result.documents_skipped += 1
        return

    # Same path, different content: this is an edit, so the previous revision is
    # tombstoned once the new one is written. A file identifies a document across
    # revisions; the content hash identifies a revision.
    previous = find_live_by_source(session, tenant_id=tenant_id, source_path=str(path))

    chunks = chunk_blocks(parsed.blocks)
    if not chunks:
        result.documents_skipped += 1
        return

    # Scanned, recorded, and NOT acted upon. This platform exists to answer
    # questions about enterprise documents, and enterprise documents contain
    # people — an HR handbook naming an employee, a support export full of
    # customer addresses. Refusing those would refuse the product.
    #
    # What this buys is an informed labelling decision: an operator can see that
    # a file carries 4,000 email addresses before choosing who may read it.
    # Counts only; carrying examples would put the PII into the very metadata
    # written to make it visible.
    pii_summary = summarise("\n".join(chunk.content for chunk in chunks))
    if pii_summary:
        logger.info("%s contains PII: %s — check its labels", path.name, pii_summary)

    document_id = session.execute(
        text("""
            INSERT INTO document
              (id, tenant_id, title, source_path, content_hash, labels, version,
               pii_summary)
            VALUES (:id, :t, :title, :src, :hash, :labels, :version,
                    CAST(:pii AS jsonb))
            RETURNING id
        """),
        {
            "id": uuid.uuid4(),
            "t": tenant_id,
            "title": parsed.title or path.stem,
            "src": str(path),
            "hash": content_hash,
            "labels": labels,
            "version": (previous[1] + 1) if previous else 1,
            "pii": json.dumps(pii_summary),
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

    if previous:
        supersede_document(
            session,
            tenant_id=tenant_id,
            old_document_id=previous[0],
            new_document_id=uuid.UUID(str(document_id)),
        )
        result.documents_superseded += 1

    result.documents_created += 1
    result.chunks_created += len(chunks)
