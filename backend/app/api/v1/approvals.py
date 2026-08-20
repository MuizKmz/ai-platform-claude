"""The approval queue: the only route from a proposal to a write.

**This is the only module permitted to import `tools.approval`.** That is not a
convention — `test_only_the_approvals_api_can_execute` scans the import graph
and fails if anything else does. The agent cannot reach the executor because it
has no way to name it.

Four operations, and the asymmetry between them is deliberate:

    GET    /approvals            see the queue           admin or the requester
    GET    /approvals/{id}/dry-run   see the exact payload    admin
    POST   /approvals/{id}/approve   approve AND execute      admin
    POST   /approvals/{id}/reject    refuse                   admin

**Approving requires admin; proposing does not.** An analyst who notices a
problem should be able to ask for a ticket; approving an action against a
production system is a different decision. Collapsing the two would either stop
analysts asking or let them self-approve, and both are worse than an extra step.

**A requester cannot approve their own request.** Even an admin. The whole
mechanism is a second pair of eyes, and an admin approving their own proposal is
one pair looking twice.

**Approve and execute are one endpoint, not two.** A queue where "approved" and
"done" are separate states needs something to move rows between them, and that
something is either a background worker nobody watches or a second button
nobody presses. Executing inline means the approver sees the outcome — and a
failure lands in front of the person who just authorised it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.api.deps import CurrentPrincipal, TenantDB
from app.core.security import Principal
from app.db.models.approval_request import ApprovalStatus
from app.tools.approval import ApprovalError, compensate, execute_approved

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["approvals"])


class ApprovalOut(BaseModel):
    id: uuid.UUID
    status: str
    tool_name: str
    connector_slug: str
    summary: str
    target_method: str
    target_path: str
    requested_by_email: str
    reason: str | None
    decided_by_email: str | None
    decided_at: datetime | None
    decision_note: str | None
    executed_at: datetime | None
    response_status: int | None
    error: str | None
    compensated_at: datetime | None
    created_at: datetime
    expires_at: datetime
    trace_id: str | None


class DryRunOut(BaseModel):
    """Exactly what would be sent, before anyone agrees to it.

    The DoD requires dry-run to show "the exact payload". This returns the
    stored payload verbatim rather than a rendering of it — a rendering is a
    second representation, and the two can disagree.
    """

    id: uuid.UUID
    status: str
    summary: str
    method: str
    path: str
    payload: dict[str, Any]
    connector_slug: str
    # Where it would actually go. "Create a ticket" means something different
    # against a staging host, and an approver cannot see that from the path.
    target_base_url: str | None
    idempotency_key: str
    requested_by_email: str
    reason: str | None
    expires_at: datetime
    # True when this request can still be approved. False for anything already
    # decided, executed, or expired.
    actionable: bool
    # Whether the target address passes the egress policy. Checked here so an
    # approver learns a connector is misconfigured BEFORE authorising an action
    # that cannot succeed — it is a DNS resolution, not a request, so nothing is
    # contacted. None when there is no base URL to check.
    target_reachable: bool | None
    target_problem: str | None


class DecisionIn(BaseModel):
    # Why, in the approver's words. Optional for an approval, because "yes, do
    # it" needs no essay; strongly encouraged for a rejection, where the next
    # person needs to know what was wrong.
    note: str | None = Field(default=None, max_length=2000)


class ExecutionOut(BaseModel):
    id: uuid.UUID
    status: str
    response_status: int
    response_body: dict[str, Any] | None
    idempotency_key: str


_SELECT = """
    SELECT id, status, tool_name, connector_slug, summary, payload, target_method,
           target_path, idempotency_key, requested_by, requested_by_email, reason,
           decided_by, decided_by_email, decided_at, decision_note, executed_at,
           response_status, error, compensated_at, created_at, expires_at, trace_id
    FROM approval_request
"""


def _require_admin(principal: Principal) -> None:
    if not principal.has_role("admin"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Approving or rejecting an action requires the admin role.",
        )


def _row_to_out(row: Any) -> ApprovalOut:
    return ApprovalOut(
        id=row.id,
        status=row.status,
        tool_name=row.tool_name,
        connector_slug=row.connector_slug,
        summary=row.summary,
        target_method=row.target_method,
        target_path=row.target_path,
        requested_by_email=row.requested_by_email,
        reason=row.reason,
        decided_by_email=row.decided_by_email,
        decided_at=row.decided_at,
        decision_note=row.decision_note,
        executed_at=row.executed_at,
        response_status=row.response_status,
        error=row.error,
        compensated_at=row.compensated_at,
        created_at=row.created_at,
        expires_at=row.expires_at,
        trace_id=row.trace_id,
    )


@router.get("/approvals", response_model=list[ApprovalOut])
def list_approvals(
    principal: CurrentPrincipal,
    db: TenantDB,
    pending_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ApprovalOut]:
    """The queue. RLS scopes it to this tenant.

    Visible to any authenticated user, not just admins: a person who proposed
    an action should be able to see what happened to it, and an approval queue
    only an admin can read is one nobody chases.

    Note what this does NOT return: the payload. Listing is for triage, and the
    payload is what dry-run is for — a queue view that dumps every request body
    puts a lot of sensitive detail on one screen for no decision-making gain.
    """
    clause = "WHERE status = 'pending'" if pending_only else ""
    rows = db.execute(
        text(f"{_SELECT} {clause} ORDER BY created_at DESC LIMIT :limit"),  # noqa: S608
        {"limit": limit},
    ).all()
    return [_row_to_out(row) for row in rows]


@router.get("/approvals/{request_id}/dry-run", response_model=DryRunOut)
def dry_run(request_id: uuid.UUID, principal: CurrentPrincipal, db: TenantDB) -> DryRunOut:
    """Exactly what would happen, without anything happening.

    Admin-only, because the payload is the sensitive part and the audience for
    it is whoever is about to decide.

    Nothing here touches the target system — the name is literal. The response
    is assembled from the stored proposal, so what an approver reads is what
    the executor will send.
    """
    _require_admin(principal)

    row = db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": request_id}).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such approval request.")

    base_url = db.execute(
        text("SELECT settings->>'base_url' FROM connector WHERE slug = :slug"),
        {"slug": row.connector_slug},
    ).scalar()

    reachable, problem = _check_target(db, row.connector_slug, base_url)

    return DryRunOut(
        id=row.id,
        status=row.status,
        summary=row.summary,
        method=row.target_method,
        path=row.target_path,
        # Verbatim. A rendering would be a second representation of the same
        # thing, and two representations can disagree.
        payload=dict(row.payload or {}),
        connector_slug=row.connector_slug,
        target_base_url=base_url,
        idempotency_key=row.idempotency_key,
        requested_by_email=row.requested_by_email,
        reason=row.reason,
        expires_at=row.expires_at,
        actionable=(row.status == ApprovalStatus.PENDING and row.expires_at > datetime.now(UTC)),
        target_reachable=reachable,
        target_problem=problem,
    )


def _check_target(db: Any, slug: str, base_url: str | None) -> tuple[bool | None, str | None]:
    """Whether the egress policy permits this connector's address.

    Resolves and validates; opens no socket. A blocked address is knowable
    before an approval is recorded, and telling an approver afterwards — via a
    502 on a request they already authorised — is telling them too late.
    """
    if not base_url:
        return None, None

    import httpx

    from app.connectors.egress import (
        EgressBlockedError,
        EgressPolicy,
        resolve_and_validate,
    )

    config = dict(
        db.execute(
            text("SELECT settings FROM connector WHERE slug = :slug"), {"slug": slug}
        ).scalar()
        or {}
    )

    parsed = httpx.URL(base_url)
    try:
        resolve_and_validate(
            parsed.host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            EgressPolicy(
                allow_private=bool(config.get("allow_private")),
                allow_loopback=bool(config.get("allow_loopback")),
            ),
        )
    except EgressBlockedError:
        return False, "The target address is not permitted by the egress policy."
    except OSError:
        return False, "The target host could not be resolved."
    return True, None


@router.post("/approvals/{request_id}/approve", response_model=ExecutionOut)
def approve(
    request_id: uuid.UUID,
    body: DecisionIn,
    principal: CurrentPrincipal,
    db: TenantDB,
) -> ExecutionOut:
    """Approve, then execute. The only path to a write in this system.

    The two are one operation deliberately. A queue where "approved" and "done"
    are separate states needs something to move rows between them, and that is
    either a worker nobody watches or a button nobody presses — both of which
    put distance between the decision and its consequence.

    Executing inline means the approver sees the outcome, and a failure lands in
    front of the person who just authorised it.
    """
    _require_admin(principal)

    # FOR UPDATE, and it is load-bearing.
    #
    # Without the lock this is a check-then-act race: three admins clicking
    # Approve at the same moment all READ `pending`, all pass the status check,
    # and all proceed to execute. Found by firing three concurrent approvals at
    # one request — all three returned 200 and all three sent a request to the
    # target. Only the idempotency key kept it to one ticket, which is a
    # backstop against a target that deduplicates, not a substitute for not
    # sending three times.
    #
    # The lock makes the losers block until the winner commits, at which point
    # they read `approved` and are refused with a 409.
    row = db.execute(
        text("""
            SELECT id, status, requested_by, summary, expires_at
            FROM approval_request WHERE id = :id
            FOR UPDATE
        """),
        {"id": request_id},
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such approval request.")

    if row.status != ApprovalStatus.PENDING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This request is already {row.status!r} and cannot be approved again.",
        )

    # Expiry is checked HERE, before an approval is recorded.
    #
    # The executor checks it too, and that is where it was originally caught —
    # but by then the row had already been marked approved, "WRITE APPROVED"
    # had been logged for something that was never approvable, and the caller
    # got a 502 implying the TARGET had failed when nothing was contacted.
    #
    # A stale proposal is a client error, not a gateway error, and the audit
    # trail should not record an approval that could never have run.
    if row.expires_at and row.expires_at < datetime.now(UTC):
        db.execute(
            text("""
                UPDATE approval_request
                SET status = :status, error = 'expired before a decision was made'
                WHERE id = :id
            """),
            {"id": request_id, "status": ApprovalStatus.EXPIRED},
        )
        # Committed before raising. `get_tenant_db` rolls back on an exception,
        # so without this the row stays `pending` and the next approver meets
        # the same stale proposal — the marking would be lost with the refusal.
        db.commit()
        logger.info("write proposal expired: request=%s tenant=%s", request_id, principal.tenant_id)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This request expired before a decision was made. Propose it again if "
            "it is still wanted — approving stale actions is how the wrong thing happens.",
        )

    # The second pair of eyes. An admin approving their own proposal is one
    # pair looking twice, which is not what the mechanism is for.
    if row.requested_by == principal.user_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You proposed this action, so you cannot approve it. Ask another admin to review it.",
        )

    # Recorded BEFORE execution. If the write succeeds and the process then
    # dies, the record already says who authorised it — the opposite order
    # would leave a completed action with no approver.
    db.execute(
        text("""
            UPDATE approval_request
            SET status = :status, decided_by = :user, decided_by_email = :email,
                decided_at = now(), decision_note = :note
            WHERE id = :id
        """),
        {
            "id": request_id,
            "status": ApprovalStatus.APPROVED,
            "user": principal.user_id,
            "email": principal.email,
            "note": body.note,
        },
    )

    # Alerting on write attempts, as the DoD requires. WARNING rather than INFO
    # because a write reaching a production system is worth noticing in a log
    # nobody is reading closely.
    logger.warning(
        "WRITE APPROVED: request=%s tenant=%s approver=%s summary=%r",
        request_id,
        principal.tenant_id,
        principal.email,
        row.summary,
    )

    try:
        result = execute_approved(db, request_id=request_id, principal=principal)
    except ApprovalError as exc:
        # The row is already marked failed by the executor. Committing keeps
        # that record — rolling back would erase the evidence that a write was
        # attempted, which is exactly what an audit trail is for.
        db.commit()
        logger.error("WRITE FAILED: request=%s reason=%s", request_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    logger.warning(
        "WRITE EXECUTED: request=%s tenant=%s approver=%s status=%s",
        request_id,
        principal.tenant_id,
        principal.email,
        result.status_code,
    )

    return ExecutionOut(
        id=request_id,
        status=ApprovalStatus.EXECUTED,
        response_status=result.status_code,
        response_body=result.response_body,
        idempotency_key=result.idempotency_key,
    )


@router.post("/approvals/{request_id}/reject", response_model=ApprovalOut)
def reject(
    request_id: uuid.UUID,
    body: DecisionIn,
    principal: CurrentPrincipal,
    db: TenantDB,
) -> ApprovalOut:
    """Refuse an action. Terminal — a rejected request cannot be revived.

    Reviving one would mean a second decision on the same row, and an audit
    trail that records only the last decision is not one. Propose it again if
    circumstances changed; the new proposal gets its own record.

    Unlike approval, rejecting your own proposal is allowed. Withdrawing a
    request you no longer want is not a privilege escalation.
    """
    _require_admin(principal)

    # Locked for the same reason as approve(): without it, two admins rejecting
    # simultaneously both pass the status check and both write a decision, and
    # the audit trail records whichever committed last.
    row = db.execute(
        text("SELECT id, status FROM approval_request WHERE id = :id FOR UPDATE"),
        {"id": request_id},
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such approval request.")

    if row.status != ApprovalStatus.PENDING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This request is already {row.status!r}.",
        )

    db.execute(
        text("""
            UPDATE approval_request
            SET status = :status, decided_by = :user, decided_by_email = :email,
                decided_at = now(), decision_note = :note
            WHERE id = :id
        """),
        {
            "id": request_id,
            "status": ApprovalStatus.REJECTED,
            "user": principal.user_id,
            "email": principal.email,
            "note": body.note,
        },
    )

    logger.info(
        "write rejected: request=%s tenant=%s by=%s note=%r",
        request_id,
        principal.tenant_id,
        principal.email,
        body.note,
    )

    updated = db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": request_id}).one()
    return _row_to_out(updated)


@router.post("/approvals/{request_id}/compensate", response_model=ExecutionOut)
def undo(request_id: uuid.UUID, principal: CurrentPrincipal, db: TenantDB) -> ExecutionOut:
    """Undo an executed write, where the target supports it.

    The row is marked compensated rather than reverted to pending: "this was
    done and then undone" and "this never happened" are different facts, and an
    audit trail that conflates them is not one.
    """
    _require_admin(principal)

    try:
        result = compensate(db, request_id=request_id, principal=principal)
    except ApprovalError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    logger.warning(
        "WRITE COMPENSATED: request=%s tenant=%s by=%s",
        request_id,
        principal.tenant_id,
        principal.email,
    )

    return ExecutionOut(
        id=request_id,
        status=ApprovalStatus.EXECUTED,
        response_status=result.status_code,
        response_body=result.response_body,
        idempotency_key=result.idempotency_key,
    )
