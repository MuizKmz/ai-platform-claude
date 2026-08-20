"""The /v1/agent HTTP contract.

The graph and tool authorization have their own tests. What is asserted here is
the boundary between them and a caller: which fields cross it, and — the point
of the file — that a refused tool call is *labelled* as one.

That last part is why this exists. The console distinguishes a denial from an
ordinary tool failure, because a model repeatedly asking for tools it may not
use is what a successful prompt injection looks like from the outside. The
frontend originally detected it by substring-matching the error message, which
would have broken silently the day that message was reworded. The `denied` flag
replaced that, and this test is what keeps it honest.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.api.v1 import agent as agent_module
from app.core.config import settings
from app.core.security import issue_token
from app.knowledge.embedding import FakeEmbeddings
from app.llm.base import Completion, Message, Usage
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

TENANT = uuid.UUID("a9e70000-0000-0000-0000-0000000000a9")
USER = uuid.UUID("a9e70000-0000-0000-0000-0000000000e7")


class ScriptedPlanner:
    """Decisions in order, so a run is the same every time it executes."""

    def __init__(self, decisions: list[dict[str, object]]) -> None:
        self._decisions = decisions
        self.calls = 0

    @property
    def model(self) -> str:
        return "scripted/planner"

    def complete(self, messages: list[Message], *, max_tokens: int = 1024) -> Completion:
        index = min(self.calls, len(self._decisions) - 1)
        self.calls += 1
        return Completion(
            text=json.dumps(self._decisions[index]),
            usage=Usage(prompt_tokens=40, completion_tokens=10),
            model=self.model,
        )

    def stream(self, messages: list[Message], *, max_tokens: int = 1024):  # noqa: ANN201
        yield self.complete(messages).text

    def count_tokens(self, text_: str) -> int:
        return max(1, len(text_) // 4)

    def cost_of(self, usage: Usage) -> float:
        return 0.0001


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def tenant(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # No test here may reach a network. The knowledge tool embeds its query,
    # and the real provider would call OpenAI.
    monkeypatch.setattr(agent_module, "get_embedding_provider", FakeEmbeddings)

    def _wipe() -> None:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM agent_run WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM trace_span WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": TENANT})

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenant (id, slug, name) VALUES (:t, 'agent-api', 'Agent API')"),
            {"t": TENANT},
        )
    yield
    _wipe()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _headers(labels: tuple[str, ...] = ("public",)) -> dict[str, str]:
    token = issue_token(
        tenant_id=TENANT,
        user_id=USER,
        email="agent@test",
        roles=("reader",),
        allowed_labels=labels,
    )
    return {"Authorization": f"Bearer {token}"}


def _plan(monkeypatch: pytest.MonkeyPatch, decisions: list[dict[str, object]]) -> ScriptedPlanner:
    planner = ScriptedPlanner(decisions)
    monkeypatch.setattr(agent_module, "get_llm", lambda: planner)
    return planner


# --- authentication ---------------------------------------------------------


def test_agent_requires_auth(client: TestClient) -> None:
    assert client.post("/v1/agent", json={"question": "hi"}).status_code == 401


# --- the denial flag --------------------------------------------------------


def test_a_refused_tool_call_is_labelled_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag exists so no caller has to read the error message to find out.

    A user holding only `public` asks for the database. The platform refuses,
    and the response must say so structurally — not merely in prose a client
    would have to pattern-match.
    """
    _plan(
        monkeypatch,
        [
            {
                "reasoning": "need the count",
                "tool": "query_database",
                "arguments": {"question": "how many orders"},
            },
            {"reasoning": "cannot reach it", "answer": "I could not access the database."},
        ],
    )

    body = client.post(
        "/v1/agent",
        json={"question": "How many orders shipped, and what is the policy?"},
        headers=_headers(("public",)),
    ).json()

    denied = [call for call in body["tool_calls"] if call["denied"]]
    assert denied, "a refused call must be flagged, not left for the client to infer"
    assert denied[0]["tool"] == "query_database"
    # The run continues rather than erroring: the model is told no and answers
    # without it. A denial is an observation, not a crash.
    assert body["answer"] is not None


def test_an_ordinary_tool_result_is_not_flagged_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The complement, so the flag means something.

    A test that only ever saw denied=True would pass against a field hardcoded
    to True.
    """
    _plan(
        monkeypatch,
        [
            {
                "reasoning": "search",
                "tool": "search_knowledge",
                "arguments": {"query": "refunds"},
            },
            {"reasoning": "done", "answer": "Refunds take five days."},
        ],
    )

    body = client.post(
        "/v1/agent",
        json={"question": "Compare the refund window with the stated policy."},
        headers=_headers(("public",)),
    ).json()

    # Asserted explicitly: without it this test passes when the question is
    # routed past the agent and no tool runs at all, which is how it first
    # failed — an empty tool_calls list trivially satisfies "none are denied".
    assert body["routed_directly"] is False
    calls = body["tool_calls"]
    assert calls, "the planner asked for a tool the principal holds"
    assert all(not call["denied"] for call in calls)


# --- the response shape the console depends on ------------------------------


def test_response_carries_every_field_the_console_renders(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the contract. Dropping a field here breaks the UI silently."""
    _plan(monkeypatch, [{"reasoning": "no tool needed", "answer": "Answered."}])

    body = client.post(
        "/v1/agent",
        json={"question": "Compare the policy and the numbers, then summarise."},
        headers=_headers(("public",)),
    ).json()

    assert set(body) >= {
        "question",
        "answer",
        "halted_reason",
        "tool_calls",
        "steps",
        "cost_usd",
        "trace_id",
        "routed_directly",
    }
    for call in body["tool_calls"]:
        assert set(call) >= {
            "tool",
            "arguments",
            "content",
            "error",
            "duration_ms",
            "denied",
        }


# --- routing ----------------------------------------------------------------


def test_a_simple_question_skips_the_agent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Routing is a cost control, so it is worth asserting it actually happens.

    `routed_directly` is what tells a reader why an answer shows no reasoning
    steps — without it, the routed path looks like an agent that did nothing.
    """
    planner = _plan(monkeypatch, [{"reasoning": "unused", "answer": "unused"}])

    body = client.post(
        "/v1/agent",
        json={"question": "How long does a refund take?"},
        headers=_headers(("public",)),
    ).json()

    assert body["routed_directly"] is True
    assert body["steps"] == 0
    assert planner.calls <= 1, "the routed path must not make a planning call"


def test_force_agent_overrides_routing(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch the console exposes as a checkbox."""
    _plan(monkeypatch, [{"reasoning": "asked to plan", "answer": "Planned."}])

    body = client.post(
        "/v1/agent",
        json={"question": "How long does a refund take?", "force_agent": True},
        headers=_headers(("public",)),
    ).json()

    assert body["routed_directly"] is False


# --- the record behind the response -----------------------------------------


def test_denied_calls_are_recorded_against_the_tenant(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, engine: Engine
) -> None:
    """The response is transient; the row is what an auditor reads later."""
    _plan(
        monkeypatch,
        [
            {
                "reasoning": "try the database",
                "tool": "query_database",
                "arguments": {"question": "how many"},
            },
            {"reasoning": "give up on it", "answer": "Could not reach the database."},
        ],
    )

    client.post(
        "/v1/agent",
        json={"question": "How many orders, and what does the handbook say?"},
        headers=_headers(("public",)),
    )

    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT denied_tool_calls, routed_directly FROM agent_run
                WHERE tenant_id = :t ORDER BY created_at DESC LIMIT 1
            """),
            {"t": TENANT},
        ).one()

    assert row.denied_tool_calls >= 1
    assert row.routed_directly is False


# --- repeated calls ---------------------------------------------------------


def test_a_repeated_call_is_flagged_and_not_re_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console shows a repeat as a repeat.

    Prompted by a live run where a question answerable from neither source drove
    six identical `search_knowledge` calls at 3.4x normal cost, inside every
    limit. Presenting those as six genuine consultations would misrepresent what
    the agent did.
    """
    _plan(
        monkeypatch,
        [
            {
                "reasoning": "search",
                "tool": "search_knowledge",
                "arguments": {"query": "orders"},
            },
            {
                "reasoning": "search again",
                "tool": "search_knowledge",
                "arguments": {"query": "orders"},
            },
            {"reasoning": "give up", "answer": "Not in the documents."},
        ],
    )

    body = client.post(
        "/v1/agent",
        json={"question": "Compare the order count with the stated policy."},
        headers=_headers(("public",)),
    ).json()

    assert body["routed_directly"] is False
    calls = body["tool_calls"]
    assert len(calls) == 2
    assert calls[0]["cached"] is False
    assert calls[1]["cached"] is True
