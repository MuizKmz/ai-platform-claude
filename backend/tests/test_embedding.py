"""Embedding provider contract, exercised through the fake."""

import pytest

from app.knowledge.embedding import (
    EmbeddingError,
    EmbeddingProvider,
    FakeEmbeddings,
    OpenAIEmbeddings,
)


def test_fake_satisfies_the_provider_protocol() -> None:
    """If the fake drifts from the interface, tests stop proving anything."""
    assert isinstance(FakeEmbeddings(), EmbeddingProvider)


def test_embeddings_are_deterministic() -> None:
    provider = FakeEmbeddings()

    assert provider.embed(["hello"]) == provider.embed(["hello"])


def test_different_text_yields_different_vectors() -> None:
    provider = FakeEmbeddings()

    first, second = provider.embed(["alpha", "beta"])

    assert first != second


def test_dimension_matches_the_column_definition() -> None:
    """chunk.embedding is VECTOR(1536); a mismatch fails at INSERT."""
    from app.db.models.chunk import EMBEDDING_DIM

    provider = FakeEmbeddings()

    assert provider.dimension == EMBEDDING_DIM
    assert all(len(v) == EMBEDDING_DIM for v in provider.embed(["a", "b"]))


def test_order_is_preserved() -> None:
    """Callers zip results against inputs, so order is part of the contract."""
    provider = FakeEmbeddings()
    texts = ["one", "two", "three"]

    vectors = provider.embed(texts)
    individually = [provider.embed([t])[0] for t in texts]

    assert vectors == individually


def test_empty_input_returns_empty() -> None:
    assert FakeEmbeddings().embed([]) == []


def test_vectors_are_normalised() -> None:
    """Unit length keeps cosine distance behaving like the real provider's."""
    (vector,) = FakeEmbeddings().embed(["some text"])

    magnitude = sum(v * v for v in vector) ** 0.5

    assert magnitude == pytest.approx(1.0, abs=1e-6)


def test_missing_api_key_fails_loudly() -> None:
    """Silently degrading to no embeddings would corrupt a corpus."""
    with pytest.raises(EmbeddingError, match="OPENAI_API_KEY"):
        OpenAIEmbeddings(api_key="")


def test_unknown_model_is_refused() -> None:
    """An unknown model means an unknown dimension, which the column cannot accept."""
    with pytest.raises(EmbeddingError, match="unknown embedding model"):
        OpenAIEmbeddings(model="not-a-real-model", api_key="sk-test")
