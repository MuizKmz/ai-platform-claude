"""OpenAI implementation of LLMProvider."""

from __future__ import annotations

from collections.abc import Iterator

from openai import APITimeoutError, OpenAI

from app.core.config import settings
from app.llm.base import Completion, LLMError, LLMTimeoutError, Message, Usage

# USD per 1M tokens, (prompt, completion). Kept here rather than computed at call
# time so a price change is one obvious edit with a date attached.
# Source: OpenAI pricing, 2026-08.
_PRICING = {
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}


class OpenAIProvider:
    """Chat completions via OpenAI."""

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        key = settings.openai_api_key if api_key is None else api_key
        if not key:
            raise LLMError("OPENAI_API_KEY is not set")
        self._model = model or settings.llm_model
        # A hung provider must fail the request, not hold a worker forever.
        self._client = OpenAI(
            api_key=key,
            timeout=settings.llm_timeout_seconds,
            max_retries=1,
        )

    @property
    def model(self) -> str:
        return f"openai/{self._model}"

    def complete(self, messages: list[Message], *, max_tokens: int = 1024) -> Completion:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": m.role, "content": m.content} for m in messages],  # type: ignore[misc]
                max_tokens=max_tokens,
                temperature=0,
            )
        except APITimeoutError as exc:
            raise LLMTimeoutError("the model did not respond in time") from exc
        except Exception as exc:  # noqa: BLE001 — provider errors are not ours to classify
            raise LLMError("generation failed") from exc

        choice = response.choices[0]
        usage = response.usage
        return Completion(
            text=choice.message.content or "",
            usage=Usage(
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
            ),
            model=self.model,
        )

    def stream(self, messages: list[Message], *, max_tokens: int = 1024) -> Iterator[str]:
        try:
            stream = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": m.role, "content": m.content} for m in messages],  # type: ignore[misc]
                max_tokens=max_tokens,
                temperature=0,
                stream=True,
            )
            for chunk in stream:
                choices = getattr(chunk, "choices", None)
                if choices and (delta := choices[0].delta.content):
                    yield delta
        except APITimeoutError as exc:
            raise LLMTimeoutError("the model did not respond in time") from exc
        except Exception as exc:  # noqa: BLE001
            raise LLMError("generation failed") from exc

    def count_tokens(self, text: str) -> int:
        """Approximate: ~4 characters per token for English.

        Deliberately not tiktoken — an exact count would pull in a large
        dependency to serve a budgeting heuristic. The provider reports the real
        usage after the call, and that is what gets billed and recorded.
        """
        return max(1, len(text) // 4)

    def cost_of(self, usage: Usage) -> float:
        prompt_price, completion_price = _PRICING.get(self._model, (0.0, 0.0))
        return (
            usage.prompt_tokens * prompt_price + usage.completion_tokens * completion_price
        ) / 1_000_000
