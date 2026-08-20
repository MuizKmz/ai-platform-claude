"""Executing an approved write. The only code in this system that can.

**Nothing imports this except `api/v1/approvals.py`.** Not the agent, not the
tool registry, not any tool. That is enforced by `test_only_the_approvals_api_
can_execute`, which greps the import graph — because the guarantee this phase
rests on is not "the agent does not call it", it is "the agent cannot".

The refusal order below is deliberate. Each check is cheap and each failure is
different, and running them in this order means the most alarming case — an
attempt to execute something no human approved — is refused before any network
call is made:

  1. the row exists, and belongs to this tenant (RLS does this, restated)
  2. its status is exactly `approved` — not pending, not already executed
  3. `decided_by` names a human
  4. it has not expired
  5. only then: build the connector, send the request

**Idempotency is carried, not generated here.** The key was written when the
proposal was created, so an approval clicked twice, a retried HTTP request, or
a worker that died mid-send all reuse the same key and the upstream
deduplicates. Generating it at execution time would defeat the entire point.

**Failure is recorded, never retried automatically.** A write that failed may or
may not have reached the target — that is what makes writes different from
reads. Retrying without knowing is how one ticket becomes three, so the row is
marked `failed` and a human decides.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.connectors.credentials import CredentialError, decrypt
from app.connectors.egress import EgressBlockedError, resolve_and_validate
from app.core.security import Principal
from app.db.models.approval_request import ApprovalStatus

logger = logging.getLogger(__name__)

# A write is not a read. It is not safely retryable, so the timeout is generous
# rather than aggressive — timing out a request that already reached the target
# creates exactly the ambiguity this module exists to avoid.
WRITE_TIMEOUT_SECONDS = 30.0


class ApprovalError(Exception):
    """The request cannot be executed. Message is safe to show a caller: it
    describes the state of their own request."""


@dataclass(frozen=True)
class ExecutionResult:
    request_id: uuid.UUID
    status_code: int
    response_body: dict[str, Any] | None
    idempotency_key: str


def execute_approved(
    session: Session,
    *,
    request_id: uuid.UUID,
    principal: Principal,
) -> ExecutionResult:
    """Perform the write described by an approved request.

    Called from exactly one place. Every refusal below happens before a socket
    opens, so an attempt to execute something unapproved costs nothing and
    reaches nothing.
    """
    row = session.execute(
        text("""
            SELECT id, tenant_id, status, connector_slug, payload, target_method,
                   target_path, idempotency_key, decided_by, decided_by_email,
                   expires_at, summary
            FROM approval_request
            WHERE id = :id
            FOR UPDATE
        """),
        {"id": request_id},
    ).first()

    # RLS already scopes this to the caller's tenant; a missing row is either
    # nonexistent or someone else's, and those are deliberately the same answer.
    if row is None:
        raise ApprovalError("No such approval request.")

    # THE check. Everything else in this phase exists to make this line true.
    if row.status not in ApprovalStatus.EXECUTABLE:
        raise ApprovalError(
            f"This request is {row.status!r} and cannot be executed. "
            f"Only an approved request may run."
        )

    # Belt and braces with the database's ck_approval_has_approver constraint.
    # A control worth having is worth having twice, and this one is the phase's
    # entire premise: no write without a recorded human.
    if row.decided_by is None:
        raise ApprovalError("This request is marked approved but names no approver. Refusing.")

    if row.expires_at and row.expires_at < datetime.now(UTC):
        _mark(session, request_id, ApprovalStatus.EXPIRED, error="expired before execution")
        raise ApprovalError(
            "This request expired before it was executed. Propose it again if it is "
            "still wanted — approving stale actions is how the wrong thing happens."
        )

    connector = _load_connector(session, row.connector_slug)

    logger.info(
        "executing approved write: request=%s tool_target=%s %s tenant=%s "
        "approved_by=%s executed_by=%s",
        request_id,
        row.connector_slug,
        f"{row.target_method} {row.target_path}",
        principal.tenant_id,
        row.decided_by_email,
        principal.email,
    )

    try:
        response = _send(connector, row)
    except EgressBlockedError as exc:
        _mark(session, request_id, ApprovalStatus.FAILED, error="address not permitted")
        raise ApprovalError("The target address is not permitted by egress policy.") from exc
    except httpx.TimeoutException as exc:
        # Deliberately NOT marked failed-and-retryable. A timed-out write may
        # have reached the target; retrying blind is how one action becomes two.
        _mark(
            session,
            request_id,
            ApprovalStatus.FAILED,
            error="timed out — the action may or may not have been performed",
        )
        raise ApprovalError(
            "The request timed out. It may or may not have been performed — "
            "check the target system before proposing it again."
        ) from exc
    except httpx.HTTPError as exc:
        _mark(session, request_id, ApprovalStatus.FAILED, error="request failed")
        raise ApprovalError("The request failed. See the approval record.") from exc

    body: dict[str, Any] | None = None
    try:
        parsed = response.json()
        body = parsed if isinstance(parsed, dict) else {"response": parsed}
    except ValueError:
        body = None

    if response.status_code >= 400:
        _mark(
            session,
            request_id,
            ApprovalStatus.FAILED,
            error=f"target returned {response.status_code}",
            response_status=response.status_code,
        )
        raise ApprovalError(f"The target system rejected the request ({response.status_code}).")

    _mark(
        session,
        request_id,
        ApprovalStatus.EXECUTED,
        response_status=response.status_code,
    )

    # Record where the created resource lives, so compensation has an address
    # to undo. Taken from the response rather than guessed: the target chooses
    # its own identifiers, and a constructed path would be a guess that fails
    # exactly when someone needs it most.
    created_id = (body or {}).get("id")
    if created_id:
        session.execute(
            text("""
                UPDATE approval_request
                SET payload = jsonb_set(payload, '{_created_id}', to_jsonb(CAST(:cid AS text)))
                WHERE id = :id
            """),
            {"id": request_id, "cid": str(created_id)},
        )

    return ExecutionResult(
        request_id=request_id,
        status_code=response.status_code,
        response_body=body,
        idempotency_key=row.idempotency_key,
    )


def _send(connector: dict[str, Any], row: Any) -> httpx.Response:
    """The only outbound write in this system.

    Egress is validated before the client is built, so a connector pointed at
    cloud metadata fails here rather than reaching it.
    """
    base_url = str(connector["settings"].get("base_url") or "")
    parsed = httpx.URL(base_url)
    resolve_and_validate(
        parsed.host,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        connector["egress"],
    )

    headers = {
        # Carried from the proposal, never generated here. This is what makes a
        # double-click harmless.
        "Idempotency-Key": row.idempotency_key,
        "Content-Type": "application/json",
    }
    if connector["credential"] is not None:
        headers["Authorization"] = f"Bearer {connector['credential'].get_secret_value()}"

    with httpx.Client(base_url=base_url, timeout=WRITE_TIMEOUT_SECONDS) as client:
        return client.request(
            row.target_method,
            row.target_path,
            json=dict(row.payload or {}),
            headers=headers,
        )


def _load_connector(session: Session, slug: str) -> dict[str, Any]:
    """Resolve a connector by slug, for this tenant. RLS scopes the query."""
    from app.connectors.egress import EgressPolicy

    row = session.execute(
        text("""
            SELECT slug, settings, credential, enabled
            FROM connector
            WHERE slug = :slug
        """),
        {"slug": slug},
    ).first()

    if row is None:
        raise ApprovalError(f"Connector {slug!r} is not configured.")
    if not row.enabled:
        raise ApprovalError(f"Connector {slug!r} is disabled.")

    credential = None
    if row.credential:
        try:
            credential = decrypt(row.credential)
        except CredentialError as exc:
            raise ApprovalError("The connector credential could not be decrypted.") from exc

    settings_dict = dict(row.settings or {})
    return {
        "slug": row.slug,
        "settings": settings_dict,
        "credential": credential,
        "egress": EgressPolicy(
            allow_private=bool(settings_dict.get("allow_private")),
            allow_loopback=bool(settings_dict.get("allow_loopback")),
        ),
    }


def _mark(
    session: Session,
    request_id: uuid.UUID,
    status: str,
    *,
    error: str | None = None,
    response_status: int | None = None,
) -> None:
    """Record the outcome. Always called, whatever happened.

    A write whose result was not recorded is worse than one that failed: nobody
    can tell afterwards whether it ran.
    """
    session.execute(
        text("""
            UPDATE approval_request
            SET status = :status,
                executed_at = now(),
                error = :error,
                response_status = :response_status
            WHERE id = :id
        """),
        {
            "id": request_id,
            "status": status,
            "error": error,
            "response_status": response_status,
        },
    )


def compensate(
    session: Session,
    *,
    request_id: uuid.UUID,
    principal: Principal,
) -> ExecutionResult:
    """Undo an executed write, where the target supports it.

    Only meaningful for a request in `executed`. The row is marked compensated
    rather than reverted to `pending`: "this was done and then undone" and "this
    never happened" are different facts, and an audit trail that conflates them
    is not one.
    """
    row = session.execute(
        text("""
            SELECT id, status, connector_slug, target_path, response_status,
                   idempotency_key, compensated_at
            FROM approval_request
            WHERE id = :id
            FOR UPDATE
        """),
        {"id": request_id},
    ).first()

    if row is None:
        raise ApprovalError("No such approval request.")
    if row.status != ApprovalStatus.EXECUTED:
        raise ApprovalError(
            f"Only an executed request can be compensated; this one is {row.status!r}."
        )
    if row.compensated_at is not None:
        raise ApprovalError("This request has already been compensated.")

    connector = _load_connector(session, row.connector_slug)
    resource = _created_resource_path(session, request_id)
    if resource is None:
        raise ApprovalError(
            "This action created no addressable resource, so it cannot be undone "
            "automatically. Reverse it in the target system."
        )

    base_url = str(connector["settings"].get("base_url") or "")
    parsed = httpx.URL(base_url)
    resolve_and_validate(
        parsed.host,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        connector["egress"],
    )

    headers = {"Content-Type": "application/json"}
    if connector["credential"] is not None:
        headers["Authorization"] = f"Bearer {connector['credential'].get_secret_value()}"

    try:
        with httpx.Client(base_url=base_url, timeout=WRITE_TIMEOUT_SECONDS) as client:
            response = client.request("DELETE", resource, headers=headers)
    except httpx.HTTPError as exc:
        raise ApprovalError("The compensating request failed.") from exc

    if response.status_code >= 400:
        raise ApprovalError(
            f"The target system rejected the compensating request ({response.status_code})."
        )

    session.execute(
        text("""
            UPDATE approval_request
            SET compensated_at = now(), compensated_by = :user
            WHERE id = :id
        """),
        {"id": request_id, "user": principal.user_id},
    )

    logger.warning(
        "compensated an executed write: request=%s by=%s",
        request_id,
        principal.email,
    )

    body: dict[str, Any] | None = None
    try:
        parsed_body = response.json()
        body = parsed_body if isinstance(parsed_body, dict) else None
    except ValueError:
        body = None

    return ExecutionResult(
        request_id=request_id,
        status_code=response.status_code,
        response_body=body,
        idempotency_key=row.idempotency_key,
    )


def _created_resource_path(session: Session, request_id: uuid.UUID) -> str | None:
    """Where the created resource lives, from the recorded response.

    Returns None when the action created nothing addressable — a status update,
    for instance. Saying so is better than guessing at a path.
    """
    row = session.execute(
        text("SELECT target_path, payload FROM approval_request WHERE id = :id"),
        {"id": request_id},
    ).first()
    if row is None:
        return None

    created_id = (row.payload or {}).get("_created_id")
    if not created_id:
        return None
    return f"{row.target_path.rstrip('/')}/{created_id}"
