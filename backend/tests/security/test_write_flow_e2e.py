"""The whole Phase 9 flow, from an agent run to a ticket.

Every other test in this phase proves one link. This proves the chain: a
question goes in, the agent proposes, a human approves, a ticket exists — and at
no point could the agent have skipped the middle step.

The most important assertion is the one after the agent runs and before anyone
approves: **zero tickets**. The agent has finished, it believes it has done
something useful, and nothing has happened.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.core.config import settings
from app.core.security import issue_token
from app.main import app

DEMO_API = "http://127.0.0.1:9100"


def _demo_api_available() -> bool:
    try:
        return httpx.get(f"{DEMO_API}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


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
    pytest.mark.skipif(not _demo_api_available(), reason="demo API not running on :9100"),
]

TENANT = uuid.UUID("e2e00000-0000-0000-0000-0000000000e2")
ANALYST_ID = uuid.UUID("e2e00000-0000-0000-0000-0000000000a1")
ADMIN_ID = uuid.UUID("e2e00000-0000-0000-0000-0000000000b2")


class ProposingPlanner:
    """A model that reaches for the write tool, as a user asking would make it."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def model(self) -> str:
        return "scripted/proposer"

    def complete(self, messages: object, *, max_tokens: int = 1024) -> object:
        from app.llm.base import Completion, Usage

        decisions = [
            {
                "reasoning": "the user asked for a ticket",
                "tool": "create_ticket",
                "arguments": {
                    "title": "Conveyor 3 stopped",
                    "body": "Line 3 halted at 14:20 with fault code E17.",
                    "priority": "urgent",
                },
            },
            {"reasoning": "proposed", "answer": "I have proposed a ticket for approval."},
        ]
        index = min(self.calls, len(decisions) - 1)
        self.calls += 1
        return Completion(
            text=json.dumps(decisions[index]),
            usage=Usage(prompt_tokens=40, completion_tokens=10),
            model=self.model,
        )

    def stream(self, messages: object, *, max_tokens: int = 1024) -> Iterator[str]:
        yield ""

    def count_tokens(self, text_: str) -> int:
        return max(1, len(text_) // 4)

    def cost_of(self, usage: object) -> float:
        return 0.0001


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def setup(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from app.api.v1 import agent as agent_module
    from app.knowledge.embedding import FakeEmbeddings

    httpx.post(f"{DEMO_API}/_reset", timeout=5)
    monkeypatch.setattr(agent_module, "get_embedding_provider", FakeEmbeddings)
    monkeypatch.setattr(agent_module, "get_llm", ProposingPlanner)
    # Enabled for this test only. It is off by default, which is the posture a
    # deployment that has not decided about writes should have.
    object.__setattr__(settings, "enabled_write_tools", "create_ticket")

    def _wipe() -> None:
        with engine.begin() as conn:
            for table in ("approval_request", "agent_run", "trace_span", "connector"):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t"),  # noqa: S608
                    {"t": TENANT},
                )
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": TENANT})

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenant (id, slug, name) VALUES (:t, 'e2e-write', 'E2E')"),
            {"t": TENANT},
        )
        conn.execute(
            text("""
                INSERT INTO connector
                  (id, tenant_id, kind, slug, display_name, required_labels, settings)
                VALUES (:id, :t, 'rest', 'demo', 'Demo ticketing', ARRAY['operations'],
                        CAST(:s AS jsonb))
            """),
            {
                "id": uuid.uuid4(),
                "t": TENANT,
                "s": (
                    '{"base_url": "http://127.0.0.1:9100", "endpoints": [], '
                    '"allow_private": true, "allow_loopback": true}'
                ),
            },
        )
    yield
    object.__setattr__(settings, "enabled_write_tools", "")
    _wipe()


def _headers(*, admin: bool = False) -> dict[str, str]:
    token = issue_token(
        tenant_id=TENANT,
        user_id=ADMIN_ID if admin else ANALYST_ID,
        email="boss@test" if admin else "analyst@test",
        roles=("admin",) if admin else ("analyst",),
        allowed_labels=("public", "operations"),
    )
    return {"Authorization": f"Bearer {token}"}


def test_the_agent_proposes_and_a_human_completes_it() -> None:
    """Question in, proposal out, human approves, ticket exists."""
    client = TestClient(app)

    # 1. The agent runs and reaches for the write tool.
    run = client.post(
        "/v1/agent",
        json={
            "question": "Raise an urgent ticket for conveyor 3, and check the handbook.",
            "force_agent": True,
        },
        headers=_headers(),
    )
    assert run.status_code == 200, run.text

    body = run.json()
    called = [c["tool"] for c in body["tool_calls"]]
    assert "create_ticket" in called, f"the agent did not propose; it called {called}"

    write_call = next(c for c in body["tool_calls"] if c["tool"] == "create_ticket")
    assert not write_call["denied"]
    # The model is told plainly that nothing happened.
    assert "PROPOSED" in write_call["content"]

    # 2. THE assertion. The agent has finished and nothing has happened.
    assert httpx.get(f"{DEMO_API}/tickets", timeout=5).json() == [], (
        "the agent created a ticket without human approval"
    )

    # 3. It is waiting in the queue.
    queue = client.get("/v1/approvals?pending_only=true", headers=_headers()).json()
    assert len(queue) == 1
    request_id = queue[0]["id"]
    assert queue[0]["status"] == "pending"

    # 4. An admin sees the exact payload before deciding.
    preview = client.get(f"/v1/approvals/{request_id}/dry-run", headers=_headers(admin=True)).json()
    assert preview["payload"]["title"] == "Conveyor 3 stopped"
    assert preview["payload"]["priority"] == "urgent"
    # Still nothing.
    assert httpx.get(f"{DEMO_API}/tickets", timeout=5).json() == []

    # 5. Approved by a different human than the one who proposed it.
    approved = client.post(
        f"/v1/approvals/{request_id}/approve",
        json={"note": "Confirmed with the line supervisor."},
        headers=_headers(admin=True),
    )
    assert approved.status_code == 200, approved.text

    # 6. Now, and only now, the ticket exists.
    tickets = httpx.get(f"{DEMO_API}/tickets", timeout=5).json()
    assert len(tickets) == 1
    assert tickets[0]["title"] == "Conveyor 3 stopped"
    assert tickets[0]["priority"] == "urgent"


def test_with_writes_disabled_the_agent_has_no_write_tool() -> None:
    """The default posture: the tool is not merely refused, it does not exist.

    A model cannot be argued into calling a tool it was never offered.
    """
    object.__setattr__(settings, "enabled_write_tools", "")
    client = TestClient(app)

    run = client.post(
        "/v1/agent",
        json={"question": "Raise a ticket, and check the handbook.", "force_agent": True},
        headers=_headers(),
    )
    assert run.status_code == 200

    assert "create_ticket" not in run.json()["available_tools"]
    assert httpx.get(f"{DEMO_API}/tickets", timeout=5).json() == []
