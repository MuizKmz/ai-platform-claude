"""The tool contract, and the registry that authorizes invocation.

The security property this file exists to provide:

    **Authorization is checked when a tool RUNS, not when it is offered.**

An agent decides which tool to call by generating text, and that text is
influenced by everything in its context — including documents it retrieved,
which may have been written by someone hostile. So "the model asked for this
tool" carries no authority whatsoever. It is a request, and the platform
decides independently whether it happens.

Concretely, that means `invoke()` re-checks the Principal against the tool's
requirements every single time, and does not care whether the tool appeared in
the model's context, whether it was offered, or whether the model has called it
successfully before in the same run.

The corollary is the descriptions: a tool the caller may not use is never
described to the model at all. That is not the control — the check at
invocation is — but it removes the temptation, and a model that never sees a
tool cannot be talked into requesting it.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.security import Principal


class ToolError(Exception):
    """A tool failed. Message reaches the model, so it must not carry
    credentials, hostnames, or upstream error text."""


class ToolAuthorizationError(ToolError):
    """The Principal may not invoke this tool.

    Distinct from ToolError so an authorization denial can be audited
    separately: a run of these is what an escalation attempt looks like, and it
    is invisible among ordinary failures.
    """


@dataclass(frozen=True)
class ToolSpec:
    """What a tool is, as data the model can be shown.

    Declared rather than derived from the function signature, because the
    description is a prompt: it is the only thing telling the model when this
    tool is the right one, and it deserves to be written rather than generated.
    """

    name: str
    description: str
    # Parameter name -> human description. Kept flat: nested schemas produce
    # nested mistakes, and every tool here takes a question or an identifier.
    parameters: dict[str, str]
    # Labels a caller must hold. Empty means nobody, matching the default-deny
    # used for documents and connectors.
    required_labels: tuple[str, ...] = ()
    # Roles a caller must hold, if any. Empty means no role requirement.
    required_roles: tuple[str, ...] = ()
    # Rough cost per invocation, for budgeting before the fact.
    estimated_cost_usd: float = 0.0


@dataclass
class ToolResult:
    """What a tool returns.

    `content` goes into the model's context, so it is text rather than a
    structure — the model reads it, and a JSON blob it has to parse is a place
    for it to go wrong.

    `error` being set does NOT mean the run fails. A dead connector should
    produce "I could not reach X" in the answer, not a hallucinated substitute,
    and that requires the failure to reach the model as information.
    """

    content: str
    error: str | None = None
    # Counts and identifiers only — never row contents. This is also what
    # lands in the trace.
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.error is not None


class Tool(ABC):
    """Something an agent can invoke."""

    @property
    @abstractmethod
    def spec(self) -> ToolSpec:
        """Identity, description, and requirements."""

    @abstractmethod
    def run(self, principal: Principal, **kwargs: Any) -> ToolResult:
        """Do the work. Called only after authorization has passed.

        Implementations still receive the Principal, because a tool that
        queries tenant data needs it for the query itself — authorization at
        the registry does not remove the need for scoping inside.
        """

    def authorize(self, principal: Principal) -> None:
        """Raise unless this Principal may invoke this tool.

        Default-deny on labels: a tool declaring none is usable by nobody.
        Declaring who may use a capability has to be a deliberate act, and an
        empty list is more often an oversight than an intention.
        """
        spec = self.spec

        if not spec.required_labels:
            raise ToolAuthorizationError(
                f"Tool {spec.name!r} declares no required labels and is therefore "
                "unusable. This is default-deny, not a misconfiguration to work around."
            )
        if not set(spec.required_labels) & set(principal.allowed_labels):
            raise ToolAuthorizationError(f"You do not have access to {spec.name!r}.")
        if spec.required_roles and not set(spec.required_roles) & set(principal.roles):
            raise ToolAuthorizationError(f"You do not have access to {spec.name!r}.")


@dataclass
class ToolRegistry:
    """The tools available to a tenant, and the only way to invoke one."""

    _by_tenant: dict[uuid.UUID, dict[str, Tool]] = field(default_factory=dict)

    def register(self, tenant_id: uuid.UUID, tool: Tool) -> None:
        self._by_tenant.setdefault(tenant_id, {})[tool.spec.name] = tool

    def available_to(self, principal: Principal) -> list[ToolSpec]:
        """Specs the Principal may actually invoke.

        Used to build the model's prompt. A tool the caller cannot use is never
        described — not as the control, but because a model that never sees a
        tool cannot be argued into requesting it.
        """
        specs: list[ToolSpec] = []
        for tool in self._by_tenant.get(principal.tenant_id, {}).values():
            try:
                tool.authorize(principal)
            except ToolAuthorizationError:
                continue
            specs.append(tool.spec)
        return specs

    def is_registered(self, tenant_id: uuid.UUID, name: str) -> bool:
        """Whether a tool of this name exists for the tenant.

        Says nothing about whether anyone may use it — deliberately. It exists
        so a refusal can be classified after the fact: a model asking for a tool
        that is not configured at all has fumbled a name, while one asking for a
        tool that exists and is denied has attempted something. Both raise the
        same error, because telling them apart in the error would let a model
        probe what is configured elsewhere.

        Not part of the authorization path. `invoke` remains the only thing that
        decides whether a call proceeds.
        """
        return name in self._by_tenant.get(tenant_id, {})

    def invoke(self, principal: Principal, name: str, **kwargs: Any) -> ToolResult:
        """Authorize, then run.

        THE security boundary of the agent. Every invocation re-checks, with no
        regard for what the model was shown, what it asked for, or what it
        called successfully thirty seconds ago. A malicious instruction inside a
        retrieved document can make the model *request* anything; it cannot make
        this method agree.
        """
        tool = self._by_tenant.get(principal.tenant_id, {}).get(name)
        if tool is None:
            # Same error whether the tool does not exist or belongs to another
            # tenant. Distinguishing them would let a model probe for what is
            # configured elsewhere.
            raise ToolAuthorizationError(f"You do not have access to {name!r}.")

        tool.authorize(principal)
        return tool.run(principal, **kwargs)


# Process-wide registry, populated at startup.
registry = ToolRegistry()
