"""Writing connector audit rows.

Every query is recorded, allowed or denied, before the caller sees a result.
Ordering matters: a row written after a successful response would be missing
exactly when the process dies mid-request, which is when it is most wanted.

Unlike a trace span, an audit failure is NOT swallowed. Losing a trace costs
observability; losing an audit row means a query ran with no record that it did,
and for a system whose whole premise is governed access that is worse than
failing the request.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import Principal


def record_query(
    session: Session,
    *,
    principal: Principal,
    connector_id: str,
    sql: str,
    allowed: bool,
    question: str | None = None,
    denial_reason: str | None = None,
    row_count: int | None = None,
    duration_ms: float | None = None,
    trace_id: str | None = None,
) -> uuid.UUID:
    """Write one audit row. Returns its id."""
    audit_id = uuid.uuid4()
    session.execute(
        text("""
            INSERT INTO connector_audit
              (id, tenant_id, user_id, user_email, connector_id, sql, question,
               allowed, denial_reason, row_count, duration_ms, trace_id)
            VALUES
              (:id, :tenant_id, :user_id, :user_email, :connector_id, :sql, :question,
               :allowed, :denial_reason, :row_count, :duration_ms, :trace_id)
        """),
        {
            "id": audit_id,
            "tenant_id": principal.tenant_id,
            "user_id": principal.user_id,
            "user_email": principal.email,
            "connector_id": connector_id,
            "sql": sql,
            "question": question,
            "allowed": allowed,
            "denial_reason": denial_reason,
            "row_count": row_count,
            "duration_ms": duration_ms,
            "trace_id": trace_id,
        },
    )
    return audit_id
