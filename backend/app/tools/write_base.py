"""Write-capable tools, which cannot write.

That is not a joke, it is the design. Read this before adding one.

**A WriteTool has no execution code.** Its `run()` builds a proposal, stores it
as a pending `approval_request`, and returns a message saying so. There is no
branch inside it that performs the write, no flag that enables one, and no
argument that skips the queue. The class physically cannot reach a target
system.

Execution lives in `app/tools/approval.py`, in a function the agent has no
reference to, called from one endpoint that requires a named human's approval to
already be recorded. The separation is the guarantee: "the developer forgot to
check for approval" is not a bug that can occur here, because there is no code
path in which the check could be omitted.

The alternative — one tool that checks for an approval and executes if it finds
one — was rejected deliberately. It concentrates the guarantee into an
`if` statement that must be correct in every write tool anyone writes later, and
that is exactly the kind of thing that gets forgotten under deadline. The
roadmap calls this the highest-risk phase; the mechanism should not depend on
remembering.

**Off by default, individually.** A write tool is registered only when its
feature flag names it. An empty flag list — the default — means no write tool
exists at all, which is the correct posture for a deployment that has not
thought about it yet.

**Business APIs only.** A write tool targets an HTTP endpoint. There is no path
from here to generated SQL, and Phase 4's read-only database role remains
untouched — `test_no_generated_sql_can_write` re-verifies that every run.
"""

from __future__ import annotations

import secrets
import uuid
from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import Principal
from app.db.models.approval_request import ApprovalStatus
from app.tools.base import Tool, ToolAuthorizationError, ToolResult, ToolSpec

# How long a proposal stays actionable. An approval queue nobody clears becomes
# a list of actions whose context has moved on, and approving one of those a
# week later is worse than losing it.
PROPOSAL_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class WriteProposal:
    """Exactly what would be sent, if a human agrees.

    Every field here is shown to the approver. `payload` is the literal request
    body — not a rendering of it, not a summary — because the summary is not
    what reaches the target system.
    """

    summary: str
    method: str
    path: str
    payload: dict[str, Any]
    # Set when the target supports undoing this. Rendered in the console so an
    # approver can see whether a mistake is recoverable before making one.
    compensating_hint: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class WriteTool(Tool):
    """A tool that proposes a write and cannot perform one.

    Subclasses implement `propose()`, which is pure: it validates arguments and
    returns a `WriteProposal`. It receives no session, no HTTP client, and no
    connector — there is nothing available to it that could reach a target
    system even by accident.
    """

    #: The connector this proposal will be executed against, by slug. Resolved
    #: at execution time rather than held here, so a tool cannot carry a live
    #: client into the agent's process.
    connector_slug: str

    def __init__(self, session: Session, connector_slug: str, labels: tuple[str, ...]) -> None:
        # The session is for RECORDING the proposal, never for performing the
        # write. It is a session against our own database, which has no route
        # to the target system.
        self._session = session
        self.connector_slug = connector_slug
        self._labels = labels

    @abstractmethod
    def propose(self, principal: Principal, **kwargs: Any) -> WriteProposal:
        """Validate arguments and describe what would be done.

        Pure by construction. If this method needs a network client to decide
        what to propose, the design is wrong — validate against the schema, not
        against the live system.
        """

    def run(self, principal: Principal, **kwargs: Any) -> ToolResult:
        """Record a proposal. Never performs the write.

        This is the whole of a write tool's behaviour. The absence of an
        execution branch is the point, and adding one here would defeat every
        other control in this phase.
        """
        proposal = self.propose(principal, **kwargs)

        request_id = uuid.uuid4()
        # Generated at PROPOSAL time, not at approval time. An approval replayed
        # twice then carries the same key, and the upstream deduplicates —
        # which is what makes a double-click harmless.
        idempotency_key = f"{request_id.hex}-{secrets.token_hex(8)}"

        self._session.execute(
            text("""
                INSERT INTO approval_request
                  (id, tenant_id, status, tool_name, connector_slug, summary,
                   payload, target_method, target_path, idempotency_key,
                   requested_by, requested_by_email, reason, trace_id, expires_at)
                VALUES
                  (:id, :tenant, :status, :tool, :connector, :summary,
                   CAST(:payload AS jsonb), :method, :path, :key,
                   :user, :email, :reason, :trace, :expires)
            """),
            {
                "id": request_id,
                # Server-derived. Invariant #1 applies here as everywhere.
                "tenant": principal.tenant_id,
                "status": ApprovalStatus.PENDING,
                "tool": self.spec.name,
                "connector": self.connector_slug,
                "summary": proposal.summary,
                "payload": _json(proposal.payload),
                "method": proposal.method,
                "path": proposal.path,
                "key": idempotency_key,
                "user": principal.user_id,
                "email": principal.email,
                "reason": kwargs.get("reason"),
                "trace": kwargs.get("trace_id"),
                "expires": datetime.now(UTC) + PROPOSAL_TTL,
            },
        )

        # What the MODEL is told. Deliberately unambiguous: a model that thinks
        # it wrote something will report to the user that it did, and the user
        # will believe them.
        return ToolResult(
            content=(
                f"PROPOSED — nothing has been done yet.\n\n"
                f"{proposal.summary}\n\n"
                f"This action requires human approval before it can run. "
                f"A request has been added to the approval queue "
                f"(reference {request_id}). Tell the user it is awaiting "
                f"approval and do not claim the action was performed."
            ),
            metadata={
                "approval_request_id": str(request_id),
                "proposed": True,
                # Never the payload. A trace is a second copy of tenant data,
                # and a proposal's payload is the most sensitive part of it.
                "target_method": proposal.method,
            },
        )

    def authorize(self, principal: Principal) -> None:
        """Label and role checks, plus one addition.

        A write tool requires a role beyond labels. Labels answer "what data may
        this person see"; proposing an action against a production system is a
        different question, and answering it with the same mechanism would mean
        anyone who can read a document can propose a write about it.
        """
        super().authorize(principal)

        if not self.spec.required_roles:
            raise ToolAuthorizationError(
                f"Write tool {self.spec.name!r} declares no required roles. "
                "A write tool must name who may propose it; labels alone are "
                "not sufficient for an action against a production system."
            )


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, sort_keys=True)


def build_spec(
    *,
    name: str,
    description: str,
    parameters: dict[str, str],
    labels: tuple[str, ...],
    roles: tuple[str, ...],
) -> ToolSpec:
    """Build a write tool's spec, with the description a model actually needs.

    The appended sentence is not decoration. Without it a model calls the tool,
    sees a success-shaped result, and tells the user the ticket was raised —
    which is false and which the user has no way to check.
    """
    return ToolSpec(
        name=name,
        description=(
            f"{description}\n\n"
            "IMPORTANT: this tool does NOT perform the action. It creates a "
            "request for human approval and returns immediately. Never tell the "
            "user the action has been done; say it is awaiting approval."
        ),
        parameters=parameters,
        required_labels=labels,
        required_roles=roles,
    )
