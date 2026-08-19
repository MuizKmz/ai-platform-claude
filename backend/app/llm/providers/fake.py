"""Deterministic LLM for tests. Never calls a network.

Its behaviour is scripted rather than intelligent, because the tests it serves
assert plumbing — citation verification, refusal handling, cost recording,
injection resistance — none of which should depend on what a real model happens
to say today.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from app.llm.base import Completion, LLMError, LLMTimeoutError, Message, Usage

REFUSAL = "I don't have enough information to answer that."


class FakeLLM:
    """Scripted responses, selected by what the prompt contains."""

    def __init__(
        self,
        *,
        response: str | None = None,
        raise_timeout: bool = False,
        raise_error: bool = False,
    ) -> None:
        self._response = response
        self._raise_timeout = raise_timeout
        self._raise_error = raise_error
        self.calls: list[list[Message]] = []

    @property
    def model(self) -> str:
        return "fake/deterministic"

    def complete(self, messages: list[Message], *, max_tokens: int = 1024) -> Completion:
        self.calls.append(messages)
        if self._raise_timeout:
            raise LLMTimeoutError("fake timeout")
        if self._raise_error:
            raise LLMError("fake failure")

        text = self._response if self._response is not None else self._default(messages)
        prompt_text = "".join(m.content for m in messages)
        return Completion(
            text=text,
            usage=Usage(
                prompt_tokens=self.count_tokens(prompt_text),
                completion_tokens=self.count_tokens(text),
            ),
            model=self.model,
        )

    def stream(self, messages: list[Message], *, max_tokens: int = 1024) -> Iterator[str]:
        completion = self.complete(messages, max_tokens=max_tokens)
        # Word-at-a-time, so a streaming test sees more than one chunk.
        for word in completion.text.split(" "):
            yield word + " "

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def cost_of(self, usage: Usage) -> float:
        # A real per-token price, so cost assertions are non-zero.
        return usage.total_tokens * 0.000001

    def _default(self, messages: list[Message]) -> str:
        """Cite the first source if any were supplied, else refuse."""
        prompt = "".join(m.content for m in messages)
        if not re.search(r"^\[1\]", prompt, re.MULTILINE):
            return REFUSAL
        return "According to the sources, refunds take five business days [1]."
