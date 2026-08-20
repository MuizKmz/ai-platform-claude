"""Concrete write tools.

Each one is a `propose()` method and nothing else. If a tool in this module ever
grows a method that opens a socket, that is the bug — the writing happens in
`approval.py`, after a human has agreed.

**Registration is opt-in per tool.** `build_write_tools` returns only the tools
named in `ENABLED_WRITE_TOOLS`, which is empty by default. A deployment that has
not decided about writes gets an agent with no write tools at all, and an agent
that has no write tool cannot propose anything — which is a stronger position
than one that can propose and is trusted not to.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import Principal
from app.tools.base import Tool, ToolSpec
from app.tools.write_base import WriteProposal, WriteTool, build_spec

# Roles permitted to PROPOSE a write. Not to approve one — approving requires
# admin, checked at the endpoint. Deliberately different: the person who notices
# a problem is often not the person authorised to act on it, and collapsing the
# two would either stop analysts asking or let them self-approve.
PROPOSER_ROLES = ("analyst", "admin")


class CreateTicketTool(WriteTool):
    """Propose creating a ticket in the connected ticketing system."""

    @property
    def spec(self) -> ToolSpec:
        return build_spec(
            name="create_ticket",
            description=(
                "Propose creating a support or maintenance ticket. Use when the "
                "user asks for something to be raised, logged, or escalated. "
                "Include enough detail in the body that someone reading the "
                "ticket alone could act on it."
            ),
            parameters={
                "title": "One line describing the issue.",
                "body": "The detail: what is wrong, what was observed, what is needed.",
                "priority": "One of: low, normal, high, urgent. Defaults to normal.",
                "assignee": "Optional. Who should pick this up.",
            },
            labels=self._labels,
            roles=PROPOSER_ROLES,
        )

    def propose(self, principal: Principal, **kwargs: Any) -> WriteProposal:
        """Validate and describe. No network, no side effects.

        Validation is strict because the payload shown to an approver is the
        payload that gets sent — there is no later step that will clean it up.
        """
        title = str(kwargs.get("title") or "").strip()
        if not title:
            raise ValueError("A ticket needs a title.")
        if len(title) > 200:
            raise ValueError("Title must be 200 characters or fewer.")

        body = str(kwargs.get("body") or "").strip()
        priority = str(kwargs.get("priority") or "normal").strip().lower()
        if priority not in {"low", "normal", "high", "urgent"}:
            raise ValueError("Priority must be one of: low, normal, high, urgent.")

        assignee = kwargs.get("assignee")
        assignee = str(assignee).strip() if assignee else None

        payload: dict[str, Any] = {
            "title": title,
            "body": body,
            "priority": priority,
        }
        if assignee:
            payload["assignee"] = assignee

        return WriteProposal(
            summary=f"Create a {priority} priority ticket: {title}",
            method="POST",
            path="/tickets",
            payload=payload,
            # Shown to the approver. Knowing a mistake is reversible changes how
            # carefully someone reads — and knowing it is NOT should change it
            # more.
            compensating_hint="This ticket can be cancelled after creation.",
        )


#: Every write tool that exists, by name. A tool absent from here cannot be
#: enabled by configuration, so a typo in the flag fails loudly at startup
#: rather than silently registering nothing.
WRITE_TOOL_TYPES: dict[str, type[WriteTool]] = {
    "create_ticket": CreateTicketTool,
}


def build_write_tools(
    session: Session,
    *,
    connector_slug: str,
    labels: tuple[str, ...],
) -> list[Tool]:
    """The write tools this deployment has enabled. Usually none.

    Raises on an unknown name rather than ignoring it: a deployment that meant
    to enable `create_ticket` and typed `create_tickets` should be told, not
    left with an agent that quietly cannot propose anything.
    """
    tools: list[Tool] = []

    for name in settings.write_tool_list:
        tool_type = WRITE_TOOL_TYPES.get(name)
        if tool_type is None:
            raise ValueError(
                f"ENABLED_WRITE_TOOLS names {name!r}, which is not a write tool. "
                f"Known: {sorted(WRITE_TOOL_TYPES)}"
            )
        tools.append(tool_type(session, connector_slug, labels))

    return tools
