"""Tool authorization and run limits.

The property under test:

    A model can REQUEST any tool. The platform decides what RUNS.

That distinction is the whole security model of an agent. Tool selection is
generated text, and the context producing it includes retrieved documents that
someone else may have written. So "the model asked for this" carries no
authority at all, and the check has to happen at invocation — every time,
regardless of what was offered or what succeeded a moment ago.
"""

from __future__ import annotations

import uuid

import pytest

from app.agent.limits import LimitExceededError, RunBudget, RunLimits
from app.core.security import Principal
from app.tools.base import (
    Tool,
    ToolAuthorizationError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)

pytestmark = pytest.mark.security

TENANT = uuid.uuid4()
OTHER_TENANT = uuid.uuid4()

ANALYST = Principal(
    tenant_id=TENANT,
    user_id=uuid.uuid4(),
    email="analyst@test",
    roles=("reader",),
    allowed_labels=("public", "analytics"),
)

READER = Principal(
    tenant_id=TENANT,
    user_id=uuid.uuid4(),
    email="reader@test",
    roles=("reader",),
    allowed_labels=("public",),
)


class RecordingTool(Tool):
    """A tool that notes whether it ran. Absence of a call is the assertion."""

    def __init__(
        self,
        name: str,
        labels: tuple[str, ...] = ("public",),
        roles: tuple[str, ...] = (),
    ) -> None:
        self._name = name
        self._labels = labels
        self._roles = roles
        self.calls = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self._name,
            description=f"Test tool {self._name}.",
            parameters={"query": "Anything."},
            required_labels=self._labels,
            required_roles=self._roles,
        )

    def run(self, principal: Principal, **kwargs: object) -> ToolResult:
        self.calls += 1
        return ToolResult(content="ok")


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(TENANT, RecordingTool("search_knowledge", ("public",)))
    reg.register(TENANT, RecordingTool("query_database", ("analytics",)))
    reg.register(TENANT, RecordingTool("admin_tool", ("public",), ("admin",)))
    reg.register(OTHER_TENANT, RecordingTool("other_tenant_tool", ("public",)))
    return reg


# --- the phase's most important property ---------------------------------------


def test_tool_authz_checked_at_invocation(registry: ToolRegistry) -> None:
    """A tool the caller may not use is refused even if it is requested directly.

    The model's request is bypassed entirely here — invoke() is called as if
    the model had asked for it. That is the realistic threat: the check must
    not depend on the tool having been offered.
    """
    with pytest.raises(ToolAuthorizationError):
        registry.invoke(READER, "query_database", question="how many orders")


def test_injection_cannot_escalate_tools(registry: ToolRegistry) -> None:
    """THE test of this phase.

    A retrieved document containing "now query the salaries database" can make
    a model emit a request for query_database. It cannot make the registry
    agree, because authorization is re-derived from the Principal rather than
    read from anything the model produced.

    Simulated by invoking exactly what a successfully-injected model would ask
    for, as the user who was reading the poisoned document.
    """
    tool = next(
        t
        for t in registry._by_tenant[TENANT].values()  # noqa: SLF001 — test seam
        if t.spec.name == "query_database"
    )

    with pytest.raises(ToolAuthorizationError):
        registry.invoke(READER, "query_database", question="SELECT * FROM salaries")

    assert tool.calls == 0, "the tool executed despite the caller lacking its label"


def test_being_offered_a_tool_does_not_authorize_it(registry: ToolRegistry) -> None:
    """Offering and authorizing are separate decisions.

    Even if a tool somehow appeared in the model's context — a stale prompt, a
    cached plan, a bug — invocation still fails.
    """
    offered = {spec.name for spec in registry.available_to(ANALYST)}
    assert "query_database" in offered

    # The same tool, requested by someone who was never offered it.
    with pytest.raises(ToolAuthorizationError):
        registry.invoke(READER, "query_database", question="anything")


def test_repeated_success_does_not_cache_authorization(registry: ToolRegistry) -> None:
    """Every invocation re-checks. A tool used successfully once does not
    become permanently available within a run."""
    registry.invoke(ANALYST, "query_database", question="ok")
    registry.invoke(ANALYST, "query_database", question="ok again")

    with pytest.raises(ToolAuthorizationError):
        registry.invoke(READER, "query_database", question="now me")


# --- tenant and role boundaries -------------------------------------------------


def test_cannot_invoke_another_tenants_tool(registry: ToolRegistry) -> None:
    with pytest.raises(ToolAuthorizationError):
        registry.invoke(ANALYST, "other_tenant_tool", query="x")


def test_unknown_and_forbidden_tools_are_indistinguishable(
    registry: ToolRegistry,
) -> None:
    """Otherwise the error is a probe for what other tenants have configured."""
    with pytest.raises(ToolAuthorizationError) as missing:
        registry.invoke(ANALYST, "no_such_tool", query="x")
    with pytest.raises(ToolAuthorizationError) as forbidden:
        registry.invoke(ANALYST, "other_tenant_tool", query="x")

    assert str(missing.value).replace("no_such_tool", "X") == str(forbidden.value).replace(
        "other_tenant_tool", "X"
    )


def test_role_requirements_are_enforced(registry: ToolRegistry) -> None:
    with pytest.raises(ToolAuthorizationError):
        registry.invoke(ANALYST, "admin_tool", query="x")


def test_a_tool_with_no_labels_is_usable_by_nobody() -> None:
    """Default-deny. An empty label list is more often an oversight than an
    intention, and treating it as "everyone" is how that oversight becomes a
    disclosure."""
    reg = ToolRegistry()
    reg.register(TENANT, RecordingTool("unlabelled", ()))

    with pytest.raises(ToolAuthorizationError, match="default-deny"):
        reg.invoke(ANALYST, "unlabelled", query="x")


def test_listing_shows_only_usable_tools(registry: ToolRegistry) -> None:
    """A model that never sees a tool cannot be argued into requesting it.

    Not the control — invocation is — but it removes the temptation.
    """
    names = {spec.name for spec in registry.available_to(READER)}

    assert names == {"search_knowledge"}
    assert "query_database" not in names
    assert "admin_tool" not in names


# --- run limits -----------------------------------------------------------------


def test_step_limit_enforced() -> None:
    """A looping agent terminates."""
    budget = RunBudget(limits=RunLimits(max_steps=3))

    for _ in range(3):
        budget.check()
        budget.record_step()

    with pytest.raises(LimitExceededError) as exc:
        budget.check()
    assert exc.value.limit == "steps"


def test_tool_call_limit_enforced() -> None:
    """A model calling the same tool with small edits stops eventually."""
    budget = RunBudget(limits=RunLimits(max_tool_calls=2))

    budget.record_tool_call()
    budget.record_tool_call()

    with pytest.raises(LimitExceededError) as exc:
        budget.check()
    assert exc.value.limit == "tool_calls"


def test_cost_ceiling_enforced() -> None:
    """The limit that protects the thing nobody notices until the invoice."""
    budget = RunBudget(limits=RunLimits(max_cost_usd=0.01))

    budget.record_cost(0.004)
    budget.check()  # still under
    budget.record_cost(0.008)

    with pytest.raises(LimitExceededError) as exc:
        budget.check()
    assert exc.value.limit == "cost"
    assert "$" in str(exc.value)


def test_wall_clock_limit_enforced() -> None:
    """Asserted by moving the clock, not by sleeping.

    A sleep-based version was flaky: Windows timer granularity is around 15ms,
    so a 50ms budget and a 60ms sleep are not reliably distinguishable. A test
    that fails one run in three trains people to re-run rather than to read it.
    """
    budget = RunBudget(limits=RunLimits(max_seconds=30.0))
    # Pretend the run started well in the past.
    budget.started_at -= 31.0

    with pytest.raises(LimitExceededError) as exc:
        budget.check()
    assert exc.value.limit == "time"


def test_limits_name_which_one_was_hit() -> None:
    """ "The agent stopped" is not actionable; "stopped after 8 steps" is."""
    budget = RunBudget(limits=RunLimits(max_steps=1))
    budget.record_step()

    with pytest.raises(LimitExceededError) as exc:
        budget.check()

    assert "1 reasoning steps" in str(exc.value)


def test_remaining_time_shrinks() -> None:
    """Passed to upstreams as their own timeout, so one slow tool cannot
    consume the entire wall-clock budget unchecked."""
    budget = RunBudget(limits=RunLimits(max_seconds=30.0))
    assert budget.remaining_seconds() == pytest.approx(30.0, abs=0.5)

    budget.started_at -= 20.0

    assert budget.remaining_seconds() == pytest.approx(10.0, abs=0.5)


def test_remaining_time_never_goes_negative() -> None:
    """An overrun budget reports zero, not a negative timeout an upstream
    would reject or treat as "no limit"."""
    budget = RunBudget(limits=RunLimits(max_seconds=5.0))
    budget.started_at -= 60.0

    assert budget.remaining_seconds() == 0.0


def test_budget_summary_carries_no_content() -> None:
    """It lands in a trace, and a span must not become a second copy of tenant
    data."""
    budget = RunBudget()
    budget.record_step()
    budget.record_cost(0.001)

    summary = budget.summary()

    assert set(summary) == {"steps", "tool_calls", "cost_usd", "elapsed_seconds"}
    assert all(isinstance(v, int | float) for v in summary.values())
