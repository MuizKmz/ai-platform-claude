"""Traces and audit, for the console.

Both tables are tenant-scoped and protected by Row-Level Security, so these
endpoints inherit isolation from the database rather than reimplementing it.
That is the point of `get_tenant_db`: the query below has no tenant predicate
because it does not need one, and forgetting to add one would return nothing
rather than everything.

**Why these are admin-only.** A trace names which tenant made a request, how
long it took and what it cost; an audit row carries the SQL somebody ran. Both
are records *about* users rather than records *for* them, and an ordinary reader
having sight of every colleague's queries is a surveillance surface nobody
asked for.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import CurrentPrincipal, TenantDB

router = APIRouter(prefix="/v1", tags=["observability"])

# A page of history. Enough to see a pattern, short of an export — the same
# reasoning as the row cap on generated SQL.
MAX_LIMIT = 200


class SpanOut(BaseModel):
    id: uuid.UUID
    trace_id: str
    span_id: str
    name: str
    duration_ms: float
    status: str
    token_cost: int | None
    attributes: dict[str, Any]
    created_at: datetime


class TraceOut(BaseModel):
    """One request, as the spans it produced."""

    trace_id: str
    started_at: datetime
    total_duration_ms: float
    status: str
    span_count: int
    spans: list[SpanOut]


class AuditOut(BaseModel):
    id: uuid.UUID
    user_email: str
    connector_id: str
    sql: str
    question: str | None
    allowed: bool
    denial_reason: str | None
    row_count: int | None
    duration_ms: float | None
    trace_id: str | None
    created_at: datetime


def _require_admin(principal: CurrentPrincipal) -> None:
    if not principal.has_role("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Viewing traces and audit history requires the admin role.",
        )


@router.get("/traces", response_model=list[TraceOut])
def list_traces(
    principal: CurrentPrincipal,
    db: TenantDB,
    limit: int = Query(default=25, ge=1, le=MAX_LIMIT),
) -> list[TraceOut]:
    """Recent traces, newest first, each with its spans.

    Grouped in Python rather than by a window function: the page size is small,
    and the alternative is a query that is harder to read than the loop below
    for no measurable gain at this scale.
    """
    _require_admin(principal)

    # RLS supplies the tenant predicate. The subquery picks the most recent
    # trace ids first, so a busy tenant does not return one span each from
    # hundreds of traces.
    rows = db.execute(
        text("""
            SELECT s.id, s.trace_id, s.span_id, s.name, s.duration_ms, s.status,
                   s.token_cost, s.attributes, s.created_at
            FROM trace_span s
            JOIN (
                SELECT trace_id, max(created_at) AS latest
                FROM trace_span
                GROUP BY trace_id
                ORDER BY latest DESC
                LIMIT :limit
            ) recent ON recent.trace_id = s.trace_id
            ORDER BY recent.latest DESC, s.created_at ASC
        """),
        {"limit": limit},
    ).all()

    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(row.trace_id, []).append(row)

    traces: list[TraceOut] = []
    for trace_id, spans in grouped.items():
        traces.append(
            TraceOut(
                trace_id=trace_id,
                started_at=min(s.created_at for s in spans),
                # Summed rather than wall-clock: the spans here are sequential
                # (retrieve, then generate), so the sum IS the elapsed time.
                # Concurrent spans would need the max of end times instead.
                total_duration_ms=round(sum(s.duration_ms for s in spans), 2),
                # One failed span makes the whole request a failure, which is
                # what someone scanning a list actually wants to know.
                status="error" if any(s.status == "error" for s in spans) else "ok",
                span_count=len(spans),
                spans=[
                    SpanOut(
                        id=s.id,
                        trace_id=s.trace_id,
                        span_id=s.span_id,
                        name=s.name,
                        duration_ms=round(s.duration_ms, 2),
                        status=s.status,
                        token_cost=s.token_cost,
                        attributes=s.attributes or {},
                        created_at=s.created_at,
                    )
                    for s in spans
                ],
            )
        )

    return traces


@router.get("/audit", response_model=list[AuditOut])
def list_audit(
    principal: CurrentPrincipal,
    db: TenantDB,
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    denied_only: bool = False,
) -> list[AuditOut]:
    """Connector query history, newest first.

    `denied_only` exists because refusals are the rows worth reading. A run of
    them is what an attempted bypass looks like, and it is invisible in a list
    dominated by successful queries.
    """
    _require_admin(principal)

    rows = db.execute(
        text("""
            SELECT id, user_email, connector_id, sql, question, allowed,
                   denial_reason, row_count, duration_ms, trace_id, created_at
            FROM connector_audit
            WHERE (:denied_only = false OR allowed = false)
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {"limit": limit, "denied_only": denied_only},
    ).all()

    return [
        AuditOut(
            id=row.id,
            user_email=row.user_email,
            connector_id=row.connector_id,
            sql=row.sql,
            question=row.question,
            allowed=row.allowed,
            denial_reason=row.denial_reason,
            row_count=row.row_count,
            duration_ms=round(row.duration_ms, 2) if row.duration_ms else None,
            trace_id=row.trace_id,
            created_at=row.created_at,
        )
        for row in rows
    ]
