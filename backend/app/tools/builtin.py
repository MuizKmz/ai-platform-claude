"""The tools an agent can choose between.

Each wraps a capability that already works standalone — that was the Phase 7
prerequisite, and it matters: a tool whose underlying capability is unproven
turns every agent bug into two problems at once.

None of them adds authorization of its own beyond declaring requirements. The
scoping is already inside what they wrap: retrieval filters by tenant and label
in its WHERE clause, the SQL connector runs as a role that physically cannot
write, and both sit behind Row-Level Security. A tool that re-implemented any
of that would be a second, weaker copy of the rules.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.connectors.base import Connector, ConnectorError
from app.core.config import settings
from app.core.security import Principal
from app.knowledge.embedding import EmbeddingProvider
from app.knowledge.retrieval import search
from app.llm.base import LLMProvider
from app.tools.base import Tool, ToolResult, ToolSpec

# How much of a chunk goes into the model's context. Whole chunks from a
# 200-page PDF would fill the window with one tool call and leave no room for
# the several the agent needs.
CHUNK_PREVIEW_CHARS = 600


class SearchKnowledgeTool(Tool):
    """Retrieve passages from the document corpus."""

    def __init__(
        self,
        session: Session,
        provider: EmbeddingProvider,
        required_labels: tuple[str, ...],
    ) -> None:
        self._session = session
        self._provider = provider
        self._required_labels = required_labels

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="search_knowledge",
            # The description is a prompt. It says when to reach for this tool
            # rather than what it does, because the model's difficulty is
            # choosing, not understanding.
            description=(
                "Search internal documents — policies, handbooks, procedures, "
                "catalogues. Use this for questions about what is written down: "
                "rules, definitions, descriptions, stated processes. Not for "
                "counts, totals, or aggregates, which live in the database."
            ),
            parameters={"query": "What to search for, in natural language."},
            required_labels=self._required_labels,
            estimated_cost_usd=0.00002,
        )

    def run(self, principal: Principal, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return ToolResult(content="", error="No query was provided.")

        hits = search(
            self._session,
            query=query,
            tenant_id=principal.tenant_id,
            allowed_labels=principal.allowed_labels,
            provider=self._provider,
            limit=settings.rag_top_k,
        )

        if not hits:
            # An explicit "nothing found" rather than an empty string. A blank
            # result invites the model to fill the silence.
            return ToolResult(
                content="No matching passages were found in the documents you can access.",
                metadata={"result_count": 0},
            )

        passages = "\n\n".join(
            f"[{i}] {hit.document_title}\n{hit.content[:CHUNK_PREVIEW_CHARS]}"
            for i, hit in enumerate(hits, start=1)
        )
        return ToolResult(
            content=passages,
            metadata={"result_count": len(hits)},
        )


class QueryDatabaseTool(Tool):
    """Answer a question by querying curated database views."""

    def __init__(
        self,
        session: Session,
        connector: Connector,
        semantics: Any,
        llm: LLMProvider,
        required_labels: tuple[str, ...],
    ) -> None:
        self._session = session
        self._connector = connector
        self._semantics = semantics
        self._llm = llm
        self._required_labels = required_labels

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="query_database",
            description=(
                "Query structured business data — orders, customers, products, "
                "line items. Use this for counts, totals, averages, rankings, "
                "and anything needing arithmetic over records. Read-only."
            ),
            parameters={
                "question": "The question to answer, in natural language.",
            },
            required_labels=self._required_labels,
            # Higher than search: this generates SQL, so it costs a model call
            # before it costs a query.
            estimated_cost_usd=0.0004,
        )

    def run(self, principal: Principal, **kwargs: Any) -> ToolResult:
        question = str(kwargs.get("question", "")).strip()
        if not question:
            return ToolResult(content="", error="No question was provided.")

        # Imported here rather than at module scope: the tool layer must not
        # make the connector layer a hard import of the agent package.
        from app.tools.query_structured_data import query_structured_data

        answer = query_structured_data(
            self._session,
            question=question,
            principal=principal,
            connector=self._connector,
            semantics=self._semantics,
            llm=self._llm,
        )

        if answer.refused or answer.result is None:
            return ToolResult(
                content=answer.error or "That question could not be answered from the database.",
                error=answer.error,
                metadata={"refused": True},
            )

        # The SQL is included because it always is — roughly one generated
        # query in five is wrong, and an answer whose query is hidden presents
        # a guess as a fact.
        rows = "\n".join(" | ".join(str(value) for value in row) for row in answer.result.rows[:25])
        content = (
            f"SQL: {answer.result.sql}\n\nColumns: {' | '.join(answer.result.columns)}\n{rows}"
        )
        return ToolResult(
            content=content,
            metadata={
                "row_count": answer.result.row_count,
                "truncated": answer.result.truncated,
            },
        )


class CallApiTool(Tool):
    """Fetch live state from a configured REST endpoint."""

    def __init__(self, connector: Connector, required_labels: tuple[str, ...]) -> None:
        self._connector = connector
        self._required_labels = required_labels

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="call_api",
            description=(
                "Fetch current state from an external system — live status, "
                "records that change minute to minute. Use this when the answer "
                "must be up to date rather than as-documented. Read-only."
            ),
            parameters={
                "endpoint": "Name of the endpoint to call.",
                "path_params": "Values for the endpoint's path parameters, if any.",
            },
            required_labels=self._required_labels,
            estimated_cost_usd=0.0,
        )

    def run(self, principal: Principal, **kwargs: Any) -> ToolResult:
        endpoint = str(kwargs.get("endpoint", "")).strip()
        if not endpoint:
            return ToolResult(content="", error="No endpoint was named.")

        path_params = kwargs.get("path_params") or {}
        if not isinstance(path_params, dict):
            path_params = {}

        try:
            result = self._connector.call(  # type: ignore[attr-defined]
                principal,
                endpoint,
                path_params={k: str(v) for k, v in path_params.items()},
            )
        except ConnectorError as exc:
            # Reported as information rather than raised. A dead connector must
            # produce "I could not reach X" in the answer, and that only happens
            # if the model learns about the failure.
            return ToolResult(
                content=f"The external system could not be reached: {exc}",
                error=str(exc),
                metadata={"endpoint": endpoint},
            )

        rows = "\n".join(" | ".join(str(value) for value in row) for row in result.rows[:25])
        return ToolResult(
            content=f"Columns: {' | '.join(result.columns)}\n{rows}",
            metadata={"row_count": result.row_count, "endpoint": endpoint},
        )


def register_builtin_tools(
    tenant_id: uuid.UUID,
    *,
    session: Session,
    embeddings: EmbeddingProvider,
    llm: LLMProvider,
    sql_connector: Connector | None = None,
    semantics: Any = None,
    rest_connector: Connector | None = None,
    knowledge_labels: tuple[str, ...] = ("public",),
    database_labels: tuple[str, ...] = ("analytics",),
    api_labels: tuple[str, ...] = ("orders",),
) -> None:
    """Populate the registry for one tenant.

    Labels are per-tenant configuration rather than constants: which label
    gates the database is a decision an operator makes, and hardcoding it here
    would put a permission boundary in the wrong file.
    """
    from app.tools.base import registry

    registry.register(tenant_id, SearchKnowledgeTool(session, embeddings, knowledge_labels))

    if sql_connector is not None and semantics is not None:
        registry.register(
            tenant_id,
            QueryDatabaseTool(session, sql_connector, semantics, llm, database_labels),
        )

    if rest_connector is not None:
        registry.register(tenant_id, CallApiTool(rest_connector, api_labels))
