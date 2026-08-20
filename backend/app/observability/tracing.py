"""Trace spans, persisted to our own Postgres.

Security invariant #6: every retrieval call emits a trace with a trace ID,
latency, and cost. Written to a table we own rather than a vendor — the durable
decision is emitting spans at all; where they are shipped is swappable later.

Attributes are a deliberate allow-list. A trace that records query text or result
content becomes a second copy of tenant data, in a table nobody thinks of as
sensitive, which is exactly how a leak survives a review of the primary store.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.pii import redact

logger = logging.getLogger(__name__)


@dataclass
class Span:
    """One traced operation."""

    trace_id: str
    span_id: str
    name: str
    tenant_id: uuid.UUID | None = None
    status: str = "ok"
    token_cost: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def set_attribute(self, key: str, value: Any) -> None:
        """Record an attribute, redacting any PII in it.

        Redaction happens HERE rather than at each call site, because this is
        the one place every attribute passes through. A call site that forgets
        is the normal case — there are dozens, and more arrive every phase — so
        the choke point has to be the thing that remembers.

        A trace is worth protecting precisely because it is easy to forget: it
        is a second copy of tenant data, retained on a different clock from the
        conversation it describes, and readable by admins who may hold none of
        the labels that gated the original.

        Only strings are scanned. Numbers and booleans are what belongs here —
        counts, durations, costs — and a caller passing a whole object is a
        design problem this cannot fix.
        """
        self.attributes[key] = redact(value) if isinstance(value, str) else value


def new_trace_id() -> str:
    """128-bit trace id, hex — the OpenTelemetry wire format."""
    return secrets.token_hex(16)


@contextmanager
def trace(
    session: Session,
    name: str,
    *,
    tenant_id: uuid.UUID | None = None,
    trace_id: str | None = None,
) -> Iterator[Span]:
    """Time an operation and persist a span for it.

    The span is written on the way out whether or not the body raised, because a
    trace that only records successes hides exactly the requests worth reading.
    """
    span = Span(
        trace_id=trace_id or new_trace_id(),
        span_id=secrets.token_hex(8),
        name=name,
        tenant_id=tenant_id,
    )
    started = time.perf_counter()
    try:
        yield span
    except Exception:
        span.status = "error"
        raise
    finally:
        duration_ms = (time.perf_counter() - started) * 1000
        try:
            session.execute(
                text("""
                    INSERT INTO trace_span
                      (id, trace_id, span_id, name, tenant_id, duration_ms, status,
                       token_cost, attributes)
                    VALUES
                      (:id, :trace_id, :span_id, :name, :tenant_id, :duration_ms, :status,
                       :token_cost, CAST(:attributes AS json))
                """),
                {
                    "id": uuid.uuid4(),
                    "trace_id": span.trace_id,
                    "span_id": span.span_id,
                    "name": span.name,
                    "tenant_id": span.tenant_id,
                    "duration_ms": duration_ms,
                    "status": span.status,
                    "token_cost": span.token_cost,
                    "attributes": _json(span.attributes),
                },
            )
        except Exception:
            # Losing a span is bad; failing a user's search because we could not
            # record one is worse. Logged, never re-raised.
            logger.exception("failed to persist trace span %s", span.name)


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, default=str)
