"""Hard limits on an agent run.

These exist from the first commit, before any loop does, because the failure
mode is not gradual. An orchestrator with no ceiling does not slowly get
expensive — it runs until something else stops it, and the thing that stops it
is usually a bill or a rate limit.

Four independent ceilings, because they fail in different ways:

    steps       — a model that keeps deciding "one more tool call"
    tool calls  — a model that calls the same tool repeatedly with small edits
    wall clock  — a slow upstream turning a request into a hang
    cost        — the one that matters to whoever pays

Any of them tripping ends the run. None of them is a soft warning: a limit that
can be exceeded is a metric, not a limit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class LimitExceededError(Exception):
    """A run hit a ceiling. Carries which one, because "the agent stopped" is
    not actionable and "the agent stopped after 8 steps" is."""

    def __init__(self, limit: str, detail: str) -> None:
        self.limit = limit
        super().__init__(detail)


@dataclass(frozen=True)
class RunLimits:
    """Ceilings for one agent run.

    The defaults are deliberately tight. A question needing more than a handful
    of steps is usually a question the agent has misunderstood, and letting it
    continue rarely recovers — it elaborates on the misunderstanding.
    """

    # Iterations of plan → act → observe.
    max_steps: int = 8
    # Total tool invocations. Lower than steps × tools because a run that calls
    # twelve tools has almost certainly stopped making progress.
    max_tool_calls: int = 12
    # Wall clock. Long enough for several model calls and a slow query, short
    # enough that a wedged run does not hold a worker indefinitely.
    max_seconds: float = 120.0
    # USD. At roughly $0.0003 per generation this is generous, and it is the
    # ceiling that protects the thing nobody notices until the invoice.
    max_cost_usd: float = 0.50


@dataclass
class RunBudget:
    """Live consumption against a set of limits.

    Mutable and passed by reference through the graph, so every node sees what
    the others have spent. A budget copied per node would let each one believe
    it had the whole allowance.
    """

    limits: RunLimits = field(default_factory=RunLimits)
    steps: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def check(self) -> None:
        """Raise if any ceiling has been reached.

        Called before doing more work rather than after: the point is to not
        spend the next unit, and a check that runs afterwards has already paid
        for what it is objecting to.
        """
        if self.steps >= self.limits.max_steps:
            raise LimitExceededError(
                "steps",
                f"Stopped after {self.steps} reasoning steps. The question may "
                "need to be asked more specifically.",
            )
        if self.tool_calls >= self.limits.max_tool_calls:
            raise LimitExceededError(
                "tool_calls",
                f"Stopped after {self.tool_calls} tool calls.",
            )
        if self.elapsed_seconds >= self.limits.max_seconds:
            raise LimitExceededError(
                "time",
                f"Stopped after {self.elapsed_seconds:.0f} seconds.",
            )
        if self.cost_usd >= self.limits.max_cost_usd:
            raise LimitExceededError(
                "cost",
                f"Stopped after ${self.cost_usd:.4f} of model usage.",
            )

    def record_step(self) -> None:
        self.steps += 1

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def record_cost(self, usd: float) -> None:
        self.cost_usd += usd

    def remaining_seconds(self) -> float:
        """Time left, for passing to an upstream as its own timeout.

        Without this a single tool can consume the entire wall-clock budget and
        the check above never gets a chance to fire.
        """
        return max(0.0, self.limits.max_seconds - self.elapsed_seconds)

    def summary(self) -> dict[str, float | int]:
        """For the trace. Counts and totals only — never inputs or outputs."""
        return {
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "cost_usd": round(self.cost_usd, 6),
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }
