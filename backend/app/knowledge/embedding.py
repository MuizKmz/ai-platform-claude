"""Embeddings behind an interface we own.

"Own your contracts, rent your implementations." The provider is swappable; the
protocol, the dimension check, and the model tag recorded on every row are not.

Every chunk stores the model and dimension that produced its vector. A corpus
embedded with two different models is silently unsearchable — cosine distance
between vectors from different models is a meaningless number, not an error — so
the only defence is knowing which rows came from where.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Protocol, runtime_checkable

from openai import OpenAI

from app.core.config import settings


class EmbeddingError(Exception):
    """The provider could not produce embeddings."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    """What the rest of the platform is allowed to assume about embeddings."""

    @property
    def model(self) -> str:
        """Provider-qualified model identifier, stored on every chunk."""
        ...

    @property
    def dimension(self) -> int:
        """Vector length. Must match the column definition or inserts fail."""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts, returning one vector per input in the same order.

        Order is part of the contract: callers zip the result against the input.
        """
        ...


class OpenAIEmbeddings:
    """Real embeddings via the OpenAI API."""

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        # `is None` rather than `or`: an explicitly-passed empty string means
        # "no key", and must not silently fall back to the configured one.
        key = settings.openai_api_key if api_key is None else api_key
        if not key:
            raise EmbeddingError(
                "OPENAI_API_KEY is not set. Add it to .env, or use FakeEmbeddings for tests."
            )
        self._model = model or settings.embedding_model
        self._client = OpenAI(api_key=key)
        dimension = _MODEL_DIMENSIONS.get(self._model)
        if dimension is None:
            raise EmbeddingError(
                f"unknown embedding model {self._model!r}; add it to _MODEL_DIMENSIONS "
                "so the vector column dimension stays in step with the model"
            )
        self._dimension: int = dimension

    @property
    def model(self) -> str:
        return f"openai/{self._model}"

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self._client.embeddings.create(model=self._model, input=texts)
        except Exception as exc:  # noqa: BLE001 — provider errors are not ours to classify
            # The message can carry request details; never let it reach a response body.
            raise EmbeddingError("embedding request failed") from exc

        # The API documents order preservation, but the whole ingestion pipeline
        # depends on it, so sort by index rather than trusting arrival order.
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [item.embedding for item in ordered]

        if len(vectors) != len(texts):
            raise EmbeddingError(f"expected {len(texts)} embeddings, received {len(vectors)}")
        for vector in vectors:
            if len(vector) != self._dimension:
                raise EmbeddingError(
                    f"model returned dimension {len(vector)}, expected {self._dimension}"
                )
        return vectors


class FakeEmbeddings:
    """Deterministic embeddings for tests. Never calls a network.

    Same text always yields the same vector, and different text yields a different
    one, which is all retrieval tests need. It is NOT semantic: "cat" and "kitten"
    are unrelated here, so this cannot be used to measure recall — that needs the
    real provider, which is why the golden-set evaluation runs separately.
    """

    def __init__(self, dimension: int = 1536, model: str = "fake/deterministic") -> None:
        self._dimension = dimension
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        # Expand a digest into a full-length vector, then normalise so cosine
        # distance behaves the way the real thing does.
        raw = bytearray()
        counter = 0
        while len(raw) < self._dimension * 4:
            raw.extend(hashlib.sha256(f"{text}:{counter}".encode()).digest())
            counter += 1
        floats = [
            struct.unpack("<i", bytes(raw[i * 4 : i * 4 + 4]))[0] / 2**31
            for i in range(self._dimension)
        ]
        norm = sum(value * value for value in floats) ** 0.5 or 1.0
        return [value / norm for value in floats]


# Dimensions are a property of the model, and the chunk.embedding column is fixed
# at 1536. Changing model means a migration and a full re-embed, not a config edit.
_MODEL_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


def get_embedding_provider() -> EmbeddingProvider:
    """The provider the application uses. Tests inject FakeEmbeddings instead."""
    return OpenAIEmbeddings()
