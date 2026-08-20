"""The agent endpoint.

Two things here are deliberate and worth stating before the code.

**Tools are built per request, not once at startup.** A tool holds a database
session, and a session bound at startup would be shared across every tenant's
requests — the session that carries `SET LOCAL app.tenant_id`, which is what
makes Row-Level Security work. Building per request costs a little construction
and buys the guarantee that a tool cannot see another tenant's rows.

**Simple questions do not use the agent.** The roadmap is explicit: *"route
simple queries directly — the agent is not free."* A planning call before a
retrieval call doubles the cost and latency of a question that never needed a
decision, so the router below sends anything without multi-step shape straight
to grounded generation.
"""

from __future__ import annotations

import logging
import re
import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.checkpointer import get_checkpointer
from app.agent.graph import run_agent
from app.agent.limits import RunLimits
from app.api.deps import CurrentPrincipal, LLMLimited, TenantDB
from app.connectors.base import ConnectorError
from app.connectors.egress import EgressBlockedError, EgressPolicy
from app.connectors.sql.connector import SQLConnector, SQLConnectorConfig
from app.connectors.sql.semantic_layer import ANALYTICS_SEMANTICS
from app.core.config import settings
from app.core.quota import record_spend
from app.core.security import Principal
from app.knowledge.embedding import get_embedding_provider
from app.llm.base import LLMProvider
from app.llm.providers.openai_provider import OpenAIProvider
from app.observability.tracing import new_trace_id, trace
from app.tools.base import ToolRegistry
from app.tools.builtin import QueryDatabaseTool, SearchKnowledgeTool
from app.tools.write_tools import build_write_tools

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["agent"])

# The connector write proposals target. One for now; a deployment with several
# write targets would carry the slug on the tool rather than here.
WRITE_CONNECTOR_SLUG = "demo"


def get_llm() -> LLMProvider:
    """One place to swap the provider. Tests patch this."""
    return OpenAIProvider()


class AgentRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    # Opt out of routing when you want to see the agent work on a simple
    # question — useful for evaluation, and honest about the cost.
    force_agent: bool = False


class ToolCallOut(BaseModel):
    tool: str
    arguments: dict[str, object]
    # The result is included because an agent answer is only checkable if you
    # can see what it was built from — the same argument as citations in chat.
    content: str
    error: str | None
    duration_ms: float
    # Whether the platform refused the call. Carried as its own field rather
    # than left for a caller to infer from the error text: a model asking for a
    # tool it is not authorized to use is the security-relevant case, and a
    # client that detects it by substring-matching an exception message breaks
    # silently the day that message is reworded.
    denied: bool = False
    # Whether this repeated an earlier identical call and was served from the
    # first result rather than re-run. Shown so a reader is not misled into
    # thinking the agent consulted a source twice.
    cached: bool = False
    # Whether the refusal was because no tool of that name exists, rather than
    # because the caller may not use it. Both raise the same error by design;
    # only this field separates a hallucinated name from an escalation attempt.
    unknown_tool: bool = False


class AgentResponse(BaseModel):
    question: str
    answer: str | None
    halted_reason: str | None
    tool_calls: list[ToolCallOut]
    steps: int
    cost_usd: float
    trace_id: str
    # True when the question was answered without the agent. Surfaced so a
    # caller can tell a routed answer from a reasoned one.
    routed_directly: bool
    # The tools this caller was actually offered.
    #
    # Lets a console tell "you may not use that" from "that tool does not
    # exist" — the model is deliberately told neither, because a distinct
    # error for a nonexistent tool would let it probe what is configured for
    # other tenants. The caller is already authenticated inside their own
    # tenant, so showing them their own tool list discloses nothing.
    available_tools: list[str]


# Words suggesting a question needs more than one source. Deliberately crude:
# the cost of routing a multi-step question to the simple path is one unhelpful
# answer, while routing every simple question to the agent doubles the bill on
# all of them.
_MULTI_STEP = re.compile(
    r"\b(and also|as well as|compare|versus|vs\.?|then|both|"
    r"how many .* and|why was|why did|what caused)\b",
    re.IGNORECASE,
)


def needs_agent(question: str) -> bool:
    """Whether a question plausibly needs more than one tool.

    A heuristic, and it will be wrong sometimes. That is acceptable in this
    direction: the agent is a fallback the caller can force, and the failure
    mode of over-routing is spending twice as much on every trivial question.
    """
    return bool(_MULTI_STEP.search(question)) or question.count("?") > 1


def build_registry(session: Session, principal: Principal, llm: LLMProvider) -> ToolRegistry:
    """Tools for one request, bound to this request's session.

    Public because the MCP server builds the same registry — that is the phase
    10 guarantee, "one chokepoint, two front doors". Named without an
    underscore so the coupling is deliberate rather than a private function
    somebody reached into.

    The session carries the tenant context that RLS reads. Sharing one across
    requests would mean a tool answering request B against request A's tenant.
    """
    registry = ToolRegistry()

    registry.register(
        principal.tenant_id,
        SearchKnowledgeTool(session, get_embedding_provider(), ("public",)),
    )

    # Write tools, if any are enabled. Usually none: ENABLED_WRITE_TOOLS is
    # empty by default, so this loop registers nothing and the agent has no way
    # to propose an action.
    #
    # Registering one does NOT give the agent the ability to write. A write tool
    # can only create a pending approval_request; the executor lives in
    # tools/approval.py, which nothing here imports and nothing here can reach.
    for write_tool in build_write_tools(
        session, connector_slug=WRITE_CONNECTOR_SLUG, labels=("operations",)
    ):
        registry.register(principal.tenant_id, write_tool)

    # The SQL connector is optional: a deployment without an analytics database
    # should still get an agent that can search documents.
    try:
        sql_connector = SQLConnector(
            SQLConnectorConfig(
                id="analytics",
                display_name="Analytics warehouse",
                host=settings.postgres_host,
                port=settings.postgres_port,
                database="analytics",
                username="analytics_readonly",
                password=SecretStr(settings.postgres_readonly_password),
                required_labels=("analytics",),
                egress=EgressPolicy(
                    allow_private=True,
                    allow_loopback=settings.allow_loopback_connectors,
                ),
            )
        )
    except EgressBlockedError:
        # Named explicitly rather than caught as a bare Exception. A security
        # control firing and a connector simply not being configured are
        # different facts, and swallowing both identically hid this for a
        # while: the agent silently had one tool instead of two.
        logger.warning(
            "analytics connector blocked by egress policy; the database tool is "
            "unavailable. Set ALLOW_LOOPBACK_CONNECTORS=true for local development."
        )
        return registry
    except (ConnectorError, OSError) as exc:
        logger.warning("analytics connector unavailable: %s", type(exc).__name__)
        return registry

    registry.register(
        principal.tenant_id,
        QueryDatabaseTool(session, sql_connector, ANALYTICS_SEMANTICS, llm, ("analytics",)),
    )
    return registry


@router.post("/agent", response_model=AgentResponse)
def ask_agent(
    request: AgentRequest,
    principal: CurrentPrincipal,
    db: TenantDB,
    # Per-tenant rate limit and daily budget. The agent's RunLimits cap one
    # run; these cap the sum of them.
    _limits: LLMLimited = None,
) -> AgentResponse:
    """Answer a question, choosing tools as needed."""
    llm = get_llm()
    trace_id = new_trace_id()

    if not request.force_agent and not needs_agent(request.question):
        # Routed to grounded generation. The agent is not free, and a question
        # with one obvious source does not need a planning call to discover it.
        return _answer_directly(db, principal, request.question, llm, trace_id)

    registry = build_registry(db, principal, llm)

    with trace(db, "agent.run", tenant_id=principal.tenant_id, trace_id=trace_id) as span:
        run = run_agent(
            request.question,
            principal=principal,
            registry=registry,
            llm=llm,
            limits=RunLimits(),
            checkpointer=get_checkpointer(),
        )
        # Counts and totals only — never tool arguments or results. A span is a
        # second copy of tenant data that nobody thinks to review.
        span.token_cost = None
        span.set_attribute("steps", run.steps)
        span.set_attribute("tool_calls", len(run.tool_calls))
        span.set_attribute("cost_usd", run.cost_usd)
        span.set_attribute("answered", run.succeeded)
        span.set_attribute(
            "denied_tool_calls",
            sum(
                1
                for c in run.tool_calls
                if c.metadata.get("denied") and not c.metadata.get("unknown_tool")
            ),
        )
        span.set_attribute(
            "unknown_tool_requests",
            sum(1 for c in run.tool_calls if c.metadata.get("unknown_tool")),
        )

    # Only refusals of tools that actually exist. A model inventing a tool name
    # is a hallucination, not an escalation attempt, and counting it here would
    # dilute the one column an auditor reads to find the real thing.
    # After the money is spent, outside the transaction.
    record_spend(principal.tenant_id, run.cost_usd)

    denied = sum(
        1 for c in run.tool_calls if c.metadata.get("denied") and not c.metadata.get("unknown_tool")
    )
    _record_run(
        db,
        principal=principal,
        thread_id=run.thread_id,
        question=run.question,
        answer=run.answer,
        halted_reason=run.halted_reason,
        steps=run.steps,
        tool_call_count=len(run.tool_calls),
        denied_tool_calls=denied,
        cost_usd=run.cost_usd,
        routed_directly=False,
        trace_id=trace_id,
    )

    return AgentResponse(
        question=run.question,
        answer=run.answer,
        halted_reason=run.halted_reason,
        tool_calls=[
            ToolCallOut(
                tool=call.tool,
                arguments=call.arguments,
                content=call.content,
                error=call.error,
                duration_ms=round(call.duration_ms, 2),
                denied=bool(call.metadata.get("denied")),
                cached=bool(call.metadata.get("cached")),
                unknown_tool=bool(call.metadata.get("unknown_tool")),
            )
            for call in run.tool_calls
        ],
        steps=run.steps,
        cost_usd=run.cost_usd,
        trace_id=trace_id,
        routed_directly=False,
        available_tools=[spec.name for spec in registry.available_to(principal)],
    )


def _record_run(
    db: Session,
    *,
    principal: Principal,
    thread_id: str,
    question: str,
    answer: str | None,
    halted_reason: str | None,
    steps: int,
    tool_call_count: int,
    denied_tool_calls: int,
    cost_usd: float,
    routed_directly: bool,
    trace_id: str,
) -> None:
    """Record the run against its tenant.

    This row is what makes a checkpoint safe to resume: LangGraph's own tables
    carry no tenant_id, so ownership of a thread is established here, in a
    table the database protects.
    """
    db.execute(
        text("""
            INSERT INTO agent_run
              (id, tenant_id, user_id, thread_id, question, answer, halted_reason,
               steps, tool_call_count, denied_tool_calls, cost_usd, routed_directly,
               trace_id)
            VALUES
              (:id, :tenant, :user, :thread, :question, :answer, :halted,
               :steps, :calls, :denied, :cost, :routed, :trace)
        """),
        {
            "id": uuid.uuid4(),
            "tenant": principal.tenant_id,
            "user": principal.user_id,
            "thread": thread_id,
            "question": question,
            "answer": answer,
            "halted": halted_reason,
            "steps": steps,
            "calls": tool_call_count,
            "denied": denied_tool_calls,
            "cost": cost_usd,
            "routed": routed_directly,
            "trace": trace_id,
        },
    )


def _answer_directly(
    db: Session,
    principal: Principal,
    question: str,
    llm: LLMProvider,
    trace_id: str,
) -> AgentResponse:
    """The simple path: retrieve, then generate, with no planning call.

    Reuses the Phase 2 machinery rather than reimplementing it, so a routed
    answer has the same citation verification and refusal behaviour as one from
    /v1/chat.
    """
    from app.knowledge.retrieval import search
    from app.llm.answering import answer_question

    with trace(db, "agent.routed", tenant_id=principal.tenant_id, trace_id=trace_id) as span:
        hits = search(
            db,
            query=question,
            tenant_id=principal.tenant_id,
            allowed_labels=principal.allowed_labels,
            provider=get_embedding_provider(),
            limit=settings.rag_top_k,
        )
        answer, usage = answer_question(question, hits, llm)
        cost = llm.cost_of(usage) if usage else 0.0
        # The routed path is cheaper, not free. Recorded here because it returns
        # before the agent path's record_spend ever runs.
        record_spend(principal.tenant_id, cost)

        span.set_attribute("result_count", len(hits))
        span.set_attribute("refused", answer.refused)
        span.set_attribute("cost_usd", cost)

    _record_run(
        db,
        principal=principal,
        thread_id=str(uuid.uuid4()),
        question=question,
        answer=answer.text,
        halted_reason=None,
        steps=0,
        tool_call_count=0,
        denied_tool_calls=0,
        cost_usd=round(cost, 6),
        routed_directly=True,
        trace_id=trace_id,
    )

    return AgentResponse(
        question=question,
        answer=answer.text,
        halted_reason=None,
        tool_calls=[],
        steps=0,
        cost_usd=round(cost, 6),
        trace_id=trace_id,
        routed_directly=True,
        # No tools were offered on this path — it never reached the agent.
        available_tools=[],
    )
