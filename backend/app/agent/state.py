"""The state that flows through the agent graph.

Deliberately small and serialisable. Every field here is written to a checkpoint
after each node, so anything that cannot survive a round trip through JSON — a
database session, a connector, a Principal object — must be passed alongside the
state rather than inside it.

That constraint is useful rather than annoying: it forces the identity to be
re-derived from a verified token on resume, instead of being restored from a
checkpoint that a restart might have carried across a permission change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict


def _append(left: list[Any], right: list[Any]) -> list[Any]:
    """Reducer for accumulating lists across nodes.

    LangGraph merges each node's returned state into the running state. Without
    a reducer the last writer wins, which would silently discard every step
    except the most recent — and the step history is the thing the trace viewer
    exists to show.
    """
    return [*left, *right]


@dataclass
class ToolCall:
    """One tool invocation and what came back.

    Kept as a record rather than folded into a message list, because the trace
    needs the structure: which tool, how long, did it fail. A flattened
    transcript loses all three.
    """

    tool: str
    arguments: dict[str, Any]
    content: str
    error: str | None = None
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.error is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "content": self.content,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCall:
        return cls(
            tool=str(data["tool"]),
            arguments=dict(data.get("arguments") or {}),
            content=str(data.get("content") or ""),
            error=data.get("error"),
            duration_ms=float(data.get("duration_ms") or 0.0),
            metadata=dict(data.get("metadata") or {}),
        )


class AgentState(TypedDict, total=False):
    """What the graph carries between nodes."""

    # The original question, unchanged throughout. The model sees its own
    # reasoning accumulate, but the question it is answering does not drift.
    question: str

    # Tool calls made so far, appended by the reducer rather than replaced.
    tool_calls: Annotated[list[dict[str, Any]], _append]

    # The model's stated reasoning for its most recent decision. Surfaced in
    # the trace: "why did it call that tool" is the commonest question when an
    # agent behaves oddly, and without this the answer is guesswork.
    plan: str

    # Set when the agent has decided it can answer. The presence of this field,
    # rather than a step count, is what ends the loop — a run that has the
    # answer should stop even if it has budget left.
    answer: str

    # Set when a run ended without an answer: a limit, a failure, a refusal.
    # Distinct from `answer` so the API can tell "here is your answer" from
    # "here is why there isn't one".
    halted_reason: str

    # Budget consumption, mirrored into state so it survives a checkpoint.
    # RunBudget itself is not serialisable and is reconstructed on resume.
    steps: int
    tool_call_count: int
    cost_usd: float

    # The tool the planner chose, handed to the act node. Declared here rather
    # than passed directly between nodes so it survives a checkpoint taken
    # between the two — a resume that lost this would re-plan and pay for it.
    pending_tool: str
    pending_args: dict[str, Any]
