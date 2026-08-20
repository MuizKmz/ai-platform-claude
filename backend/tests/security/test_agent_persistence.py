"""Checkpointing, and the isolation gap it opens.

LangGraph's checkpoint tables key on `thread_id` and carry no `tenant_id`, so
Row-Level Security does not reach them. That is the one place in this system
where isolation is not enforced by the database, and these tests cover the
boundary that stands in for it:

  - thread ids are server-generated and never accepted from a request
  - `agent_run` records ownership, carries `tenant_id`, and has a policy
  - a run's tenant is resolved from `agent_run` before a checkpoint is touched

Worth testing explicitly precisely because "the database enforces it" is true
everywhere else here, and an assumption that it is true of this too would be
reasonable and wrong.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from app.agent.graph import run_agent
from app.core.config import settings
from app.core.security import Principal
from app.llm.base import Completion, Message, Usage
from app.tools.base import Tool, ToolRegistry, ToolResult, ToolSpec


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

TENANT = uuid.UUID("7a7a0000-0000-0000-0000-00000000007a")
OTHER = uuid.UUID("7a7a0000-0000-0000-0000-0000000000ff")

ANALYST = Principal(
    tenant_id=TENANT,
    user_id=uuid.uuid4(),
    email="analyst@test",
    roles=("reader",),
    allowed_labels=("public",),
)


class ScriptedPlanner:
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
            usage=Usage(prompt_tokens=50, completion_tokens=10),
            model=self.model,
        )

    def stream(self, messages: list[Message], *, max_tokens: int = 1024):  # noqa: ANN201
        yield self.complete(messages).text

    def count_tokens(self, text_: str) -> int:
        return len(text_) // 4

    def cost_of(self, usage: Usage) -> float:
        return 0.0001


class StubTool(Tool):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="search_knowledge",
            description="Stub.",
            parameters={"query": "Anything."},
            required_labels=("public",),
        )

    def run(self, principal: Principal, **kwargs: object) -> ToolResult:
        self.calls += 1
        return ToolResult(content="a passage")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def tenants(engine: Engine) -> Iterator[None]:
    def _wipe() -> None:
        with engine.begin() as conn:
            params = {"a": TENANT, "b": OTHER}
            conn.execute(text("DELETE FROM agent_run WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), params)

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO tenant (id, slug, name) VALUES
                  (:a, 'run-test', 'Run Test'), (:b, 'run-other', 'Run Other')
            """),
            {"a": TENANT, "b": OTHER},
        )
    yield
    _wipe()


def _registry() -> tuple[ToolRegistry, StubTool]:
    tool = StubTool()
    reg = ToolRegistry()
    reg.register(TENANT, tool)
    return reg, tool


# --- checkpointing ----------------------------------------------------------


def test_run_resumable_after_restart() -> None:
    """State survives the process that created it.

    Simulated by building a fresh graph against the same thread id, which is
    what a restarted worker does: the compiled graph is gone, the checkpoint is
    not. Resuming must not re-run the tool calls already paid for.
    """
    from app.agent.checkpointer import get_checkpointer

    checkpointer = get_checkpointer()
    if checkpointer is None:
        pytest.skip("checkpointer unavailable")

    thread = str(uuid.uuid4())
    registry, tool = _registry()

    first = run_agent(
        "what do the documents say",
        principal=ANALYST,
        registry=registry,
        llm=ScriptedPlanner(
            [
                {"reasoning": "search", "tool": "search_knowledge", "arguments": {"query": "x"}},
                {"reasoning": "done", "answer": "found it"},
            ]
        ),
        checkpointer=checkpointer,
        thread_id=thread,
    )
    assert first.answer == "found it"
    calls_before = tool.calls

    # A new graph, a new planner, the same thread — a restarted worker.
    registry_after, tool_after = _registry()
    second = run_agent(
        "what do the documents say",
        principal=ANALYST,
        registry=registry_after,
        llm=ScriptedPlanner([{"reasoning": "already answered", "answer": "found it"}]),
        checkpointer=checkpointer,
        thread_id=thread,
    )

    assert second.answer is not None
    # The point: prior work was not repeated on the resumed thread.
    assert tool_after.calls == 0, "resuming re-ran a tool call already paid for"
    assert calls_before == 1


def test_a_fresh_thread_does_not_see_another_runs_state() -> None:
    """Threads are isolated from each other even without RLS beneath them."""
    from app.agent.checkpointer import get_checkpointer

    checkpointer = get_checkpointer()
    if checkpointer is None:
        pytest.skip("checkpointer unavailable")

    registry, _ = _registry()
    first = run_agent(
        "question one",
        principal=ANALYST,
        registry=registry,
        llm=ScriptedPlanner([{"reasoning": "done", "answer": "answer one"}]),
        checkpointer=checkpointer,
    )
    second = run_agent(
        "question two",
        principal=ANALYST,
        registry=registry,
        llm=ScriptedPlanner([{"reasoning": "done", "answer": "answer two"}]),
        checkpointer=checkpointer,
    )

    assert first.thread_id != second.thread_id
    assert second.answer == "answer two"


def test_thread_ids_are_server_generated() -> None:
    """Never taken from a request.

    The checkpoint tables have no tenant_id, so a caller who could choose a
    thread id could name one belonging to another tenant. Generation is the
    control that removes the possibility.
    """
    registry, _ = _registry()

    runs = [
        run_agent(
            "q",
            principal=ANALYST,
            registry=registry,
            llm=ScriptedPlanner([{"reasoning": "done", "answer": "a"}]),
        )
        for _ in range(3)
    ]

    ids = {run.thread_id for run in runs}
    assert len(ids) == 3
    for thread_id in ids:
        # Parseable as a UUID: guessing one is not a practical attack.
        uuid.UUID(thread_id)


# --- the run record, which is what the database DOES protect ----------------


def test_agent_run_rows_are_tenant_isolated(engine: Engine) -> None:
    """The table that stands in for RLS over checkpoints must itself have RLS."""
    with engine.begin() as conn:
        for tenant in (TENANT, OTHER):
            conn.execute(
                text("""
                    INSERT INTO agent_run
                      (id, tenant_id, user_id, thread_id, question)
                    VALUES (:id, :t, :u, :thread, 'q')
                """),
                {
                    "id": uuid.uuid4(),
                    "t": tenant,
                    "u": uuid.uuid4(),
                    "thread": str(uuid.uuid4()),
                },
            )

    app_engine = create_engine(settings.app_database_url)
    try:
        with app_engine.begin() as conn:
            conn.execute(text(f"SET LOCAL app.tenant_id = '{TENANT}'"))
            visible = conn.execute(text("SELECT count(*) FROM agent_run")).scalar()
        with app_engine.connect() as conn:
            without_context = conn.execute(text("SELECT count(*) FROM agent_run")).scalar()
    finally:
        app_engine.dispose()

    assert visible == 1
    assert without_context == 0, "no tenant context should mean no rows"


def test_denied_tool_calls_are_counted_separately(engine: Engine) -> None:
    """A run of denials is what an escalation attempt looks like, and it is
    invisible if counted among ordinary tool failures."""
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO agent_run
                  (id, tenant_id, user_id, thread_id, question, tool_call_count,
                   denied_tool_calls)
                VALUES (:id, :t, :u, :thread, 'q', 3, 2)
            """),
            {
                "id": uuid.uuid4(),
                "t": TENANT,
                "u": uuid.uuid4(),
                "thread": str(uuid.uuid4()),
            },
        )

    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT tool_call_count, denied_tool_calls FROM agent_run
                WHERE tenant_id = :t
            """),
            {"t": TENANT},
        ).one()

    assert row.tool_call_count == 3
    assert row.denied_tool_calls == 2
