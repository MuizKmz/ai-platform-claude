"""The LLM contract we own.

Providers are rented; this interface is not. Everything above it — the chat
endpoint, citation verification, cost accounting — depends only on what is
declared here, so swapping provider is a new file rather than a project.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class LLMError(Exception):
    """Generation failed. Deliberately opaque: provider messages can carry
    request content, and this exception reaches an error path."""


class LLMTimeoutError(LLMError):
    """The provider did not respond within the configured budget."""


@dataclass(frozen=True)
class Usage:
    """Token accounting for one call. Recorded per tenant, per request."""

    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class Completion:
    """A finished generation plus what it cost."""

    text: str
    usage: Usage
    model: str


@dataclass(frozen=True)
class Message:
    """One turn. `role` is "system", "user", or "assistant"."""

    role: str
    content: str


@runtime_checkable
class LLMProvider(Protocol):
    """What the platform may assume about any language model."""

    @property
    def model(self) -> str:
        """Provider-qualified model id, recorded on every message and span."""
        ...

    def complete(self, messages: list[Message], *, max_tokens: int = 1024) -> Completion:
        """Generate a full response."""
        ...

    def stream(self, messages: list[Message], *, max_tokens: int = 1024) -> Iterator[str]:
        """Yield response text incrementally.

        Streaming exists for perceived latency, not throughput — a user reading
        the first sentence does not care that the last is still generating.
        """
        ...

    def count_tokens(self, text: str) -> int:
        """Approximate token count, for budgeting before a call is made."""
        ...

    def cost_of(self, usage: Usage) -> float:
        """Cost in USD. Prompt and completion tokens price differently, which is
        why Usage keeps them separate rather than storing a single total."""
        ...
