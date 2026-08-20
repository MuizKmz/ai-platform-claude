"""Rate limits and per-tenant budgets.

Two of the roadmap's Phase 8 tests live here: `test_rate_limit_enforced` and
`test_quota_blocks_over_budget_tenant`.

The interesting cases are not the happy paths. They are:

  - **Every spending path records.** Chat has three ways out — buffered,
    streamed, and refused — and the agent has two. Wiring the quota into the
    obvious one and missing the others produces a budget a tenant escapes by
    changing a query parameter. Streaming was exactly that: it computed no cost
    at all, so a tenant that always streamed consumed no quota.

  - **The two limiters fail in OPPOSITE directions.** `ratelimit` fails closed
    because it guards an SSRF primitive; `quota` fails open because it guards a
    bill and an outage is worse than a day's overspend. Both are asserted, since
    a later refactor "unifying" them would silently break one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.core.config import settings
from app.core.quota import (
    QuotaExceededError,
    check_quota,
    quota_status,
    record_spend,
)
from app.core.ratelimit import RateLimit, RateLimitExceededError, check_rate_limit
from app.core.security import issue_token
from app.knowledge.embedding import FakeEmbeddings
from app.main import app


def _database_available() -> bool:
    try:
        engine = create_engine(settings.database_url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


pytestmark = [
    pytest.mark.security,
    pytest.mark.skipif(not _database_available(), reason="no database reachable"),
]

TENANT = uuid.UUID("b0d00000-0000-0000-0000-0000000000b0")
OTHER = uuid.UUID("b0d00000-0000-0000-0000-0000000000ff")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean_counters() -> Iterator[None]:
    """Counters are shared state between tests, and a fixed window does not
    forget on its own."""
    from app.db.session import redis_client

    def _clear() -> None:
        import contextlib

        with contextlib.suppress(Exception):
            for tenant in (TENANT, OTHER):
                for key in redis_client.scan_iter(f"*{tenant}*"):
                    redis_client.delete(key)

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def no_real_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here may reach OpenAI.

    These assert rate limiting and budgets, not generation quality — and CI has
    no API key, so a test that constructs the real provider fails there for a
    reason unrelated to what it checks. Three of them did, silently, from
    Phase 8 stage 1 until an audit ran the suite with the key unset.
    """
    from app.api.v1 import chat as chat_module
    from app.llm.providers.fake import FakeLLM

    monkeypatch.setattr(chat_module, "get_llm", lambda: FakeLLM(response="An answer [1]."))
    monkeypatch.setattr(chat_module, "get_embedding_provider", FakeEmbeddings)


@pytest.fixture(autouse=True)
def tenants(engine: Engine) -> Iterator[None]:
    def _wipe() -> None:
        with engine.begin() as conn:
            params = {"a": TENANT, "b": OTHER}
            conn.execute(text("DELETE FROM trace_span WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(
                text("DELETE FROM conversation_message WHERE tenant_id IN (:a, :b)"), params
            )
            conn.execute(text("DELETE FROM conversation WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM chunk WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM document WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), params)

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO tenant (id, slug, name) VALUES
                  (:a, 'quota-test', 'Quota Test'), (:b, 'quota-other', 'Quota Other')
            """),
            {"a": TENANT, "b": OTHER},
        )
        # A retrievable document for each tenant. Without one, chat refuses
        # before calling the model — correct behaviour, and it would make the
        # streaming test measure a refusal rather than a generation.
        for tenant in (TENANT, OTHER):
            _seed_document(conn, tenant)
    yield
    _wipe()


def _seed_document(conn: object, tenant: uuid.UUID) -> None:
    provider = FakeEmbeddings()
    content = "Refunds are processed within five business days of approval."
    (vector,) = provider.embed([content])
    doc_id = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text("""
            INSERT INTO document (id, tenant_id, title, source_path, content_hash, labels)
            VALUES (:id, :t, 'refunds', :src, :hash, ARRAY['public'])
        """),
        {"id": doc_id, "t": tenant, "src": f"/{tenant}", "hash": uuid.uuid4().hex},
    )
    conn.execute(  # type: ignore[attr-defined]
        text("""
            INSERT INTO chunk
              (id, tenant_id, document_id, ordinal, content, embedding,
               embedding_model, embedding_dim)
            VALUES (:id, :t, :d, 0, :content, :emb, :model, :dim)
        """),
        {
            "id": uuid.uuid4(),
            "t": tenant,
            "d": doc_id,
            "content": content,
            "emb": str(vector),
            "model": provider.model,
            "dim": provider.dimension,
        },
    )


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _headers(tenant: uuid.UUID = TENANT) -> dict[str, str]:
    token = issue_token(
        tenant_id=tenant,
        user_id=uuid.uuid4(),
        email="quota@test",
        roles=("reader",),
        allowed_labels=("public",),
    )
    return {"Authorization": f"Bearer {token}"}


# --- the roadmap's named tests ------------------------------------------------


def test_rate_limit_enforced() -> None:
    """A fixed window admits `limit` and refuses the next."""
    policy = RateLimit(limit=3, window_seconds=60)
    key = f"test-enforced:{uuid.uuid4()}"

    for _ in range(policy.limit):
        check_rate_limit(key, policy)

    with pytest.raises(RateLimitExceededError) as caught:
        check_rate_limit(key, policy)

    # It must say when to try again, or callers write tight retry loops.
    assert caught.value.retry_after_seconds > 0


def test_quota_blocks_over_budget_tenant() -> None:
    """Spend at or above the ceiling refuses the next request."""
    record_spend(TENANT, settings.tenant_daily_budget_usd)

    with pytest.raises(QuotaExceededError) as caught:
        check_quota(TENANT)

    assert caught.value.limit_usd == settings.tenant_daily_budget_usd
    # The message must name the reset, since a caller cannot otherwise know
    # whether waiting helps.
    assert "Resets at" in str(caught.value)


def test_a_tenant_under_budget_is_allowed() -> None:
    """The complement, so the test above is not satisfied by refusing always."""
    record_spend(TENANT, settings.tenant_daily_budget_usd / 10)
    status = check_quota(TENANT)
    assert not status.exhausted
    assert status.remaining_usd > 0


# --- isolation ---------------------------------------------------------------


def test_one_tenants_spend_does_not_touch_another() -> None:
    """A shared counter would let one tenant exhaust everyone's budget."""
    record_spend(TENANT, settings.tenant_daily_budget_usd)

    assert quota_status(TENANT).exhausted
    assert not quota_status(OTHER).exhausted


def test_rate_limits_are_scoped_by_key() -> None:
    policy = RateLimit(limit=2, window_seconds=60)
    key_a, key_b = f"a:{uuid.uuid4()}", f"b:{uuid.uuid4()}"

    check_rate_limit(key_a, policy)
    check_rate_limit(key_a, policy)
    with pytest.raises(RateLimitExceededError):
        check_rate_limit(key_a, policy)

    # A separate key is untouched.
    check_rate_limit(key_b, policy)


# --- windows -----------------------------------------------------------------


def test_spend_is_scoped_to_a_utc_day() -> None:
    """Yesterday's spend must not count against today.

    Asserted by moving the clock rather than by sleeping — the same reason the
    agent's timing test moves its clock: a real wait is slow and, on Windows,
    imprecise enough to flake.
    """
    yesterday = datetime.now(UTC) - timedelta(days=1)
    record_spend(TENANT, settings.tenant_daily_budget_usd, now=yesterday)

    assert quota_status(TENANT, now=yesterday).exhausted
    assert not quota_status(TENANT).exhausted, "yesterday's spend leaked into today"


def test_the_reset_time_is_the_next_midnight_utc() -> None:
    """Computed by adding a day to the day's start, not by incrementing the day
    number — which breaks on the 31st."""
    on_the_31st = datetime(2026, 1, 31, 23, 30, tzinfo=UTC)
    status = quota_status(TENANT, now=on_the_31st)
    assert status.resets_at == datetime(2026, 2, 1, 0, 0, tzinfo=UTC)


# --- the two failure directions ----------------------------------------------


def test_the_rate_limiter_fails_closed() -> None:
    """It guards an SSRF primitive: an outage is exactly when an attacker
    benefits from the limiter being open."""
    broken = MagicMock()
    broken.pipeline.side_effect = redis.ConnectionError("down")

    with pytest.raises(RateLimitExceededError):
        check_rate_limit("anything", RateLimit(limit=100, window_seconds=60), client=broken)


def test_the_quota_fails_open() -> None:
    """It guards a bill. Denying every request because a cache is down turns a
    billing control into an outage, and one window of overspend is cheaper.

    Deliberately the opposite of the test above; a refactor unifying the two
    would break one of them, which is the point of asserting both.
    """
    broken = MagicMock()
    broken.get.side_effect = redis.ConnectionError("down")

    status = quota_status(TENANT, client=broken)
    assert not status.exhausted
    assert status.spent_usd == 0.0


# --- every spending path records ---------------------------------------------


def test_chat_is_rate_limited(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Endpoints that call a model are throttled per tenant."""
    monkeypatch.setattr(settings, "llm_requests_per_minute", 2)

    statuses = [
        client.post("/v1/chat", json={"question": "hello"}, headers=_headers()).status_code
        for _ in range(4)
    ]
    assert 429 in statuses, "the chat endpoint was never throttled"


def test_an_over_budget_tenant_is_refused_with_402(client: TestClient) -> None:
    """402 rather than 429: retrying a 429 succeeds shortly, retrying this does
    not, and the status should tell them apart."""
    record_spend(TENANT, settings.tenant_daily_budget_usd)

    response = client.post("/v1/chat", json={"question": "hello"}, headers=_headers())
    assert response.status_code == 402
    assert "budget" in response.json()["detail"].lower()


def test_being_over_budget_does_not_affect_another_tenant(client: TestClient) -> None:
    record_spend(TENANT, settings.tenant_daily_budget_usd)

    blocked = client.post("/v1/chat", json={"question": "hi"}, headers=_headers(TENANT))
    other = client.post("/v1/chat", json={"question": "hi"}, headers=_headers(OTHER))

    assert blocked.status_code == 402
    assert other.status_code != 402


def test_the_limit_check_runs_before_any_work(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused request must cost nothing.

    Asserted by making the model provider explode: if the 402 arrives anyway,
    nothing downstream of the check ran.
    """
    from app.api.v1 import chat as chat_module

    def _explode() -> None:
        raise AssertionError("the provider was constructed for a refused request")

    monkeypatch.setattr(chat_module, "get_llm", _explode)
    record_spend(TENANT, settings.tenant_daily_budget_usd)

    response = client.post("/v1/chat", json={"question": "hello"}, headers=_headers())
    assert response.status_code == 402


def test_the_streaming_path_also_spends_quota(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found while wiring this up: the streaming path computed no cost at all.

    A tenant that always streamed would have consumed no quota, which makes the
    budget a setting rather than a control. Streaming yields no Usage object, so
    the cost is estimated from the prompt and the collected text — an estimate,
    and a far better one than zero.
    """
    from app.api.v1 import chat as chat_module
    from app.llm.providers.fake import FakeLLM

    monkeypatch.setattr(chat_module, "get_llm", lambda: FakeLLM(response="An answer [1]."))
    monkeypatch.setattr(chat_module, "get_embedding_provider", FakeEmbeddings)

    before = quota_status(TENANT).spent_usd

    with client.stream(
        "POST",
        "/v1/chat",
        json={"question": "hello", "stream": True},
        headers=_headers(),
    ) as response:
        # The generator only runs as the body is consumed, and record_spend
        # happens at its end — so the stream must be drained before asserting.
        for _ in response.iter_lines():
            pass

    after = quota_status(TENANT).spent_usd
    assert after > before, "a streamed request spent nothing against the budget"
