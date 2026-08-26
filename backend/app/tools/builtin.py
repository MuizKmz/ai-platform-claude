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

import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.connectors.base import Connector, ConnectorError, QueryResult
from app.core.config import settings
from app.core.security import Principal
from app.knowledge.embedding import EmbeddingProvider
from app.knowledge.retrieval import search
from app.llm.base import LLMProvider
from app.observability.audit import record_query
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
        *,
        tool_name: str = "query_database",
    ) -> None:
        self._session = session
        self._connector = connector
        self._semantics = semantics
        self._llm = llm
        self._required_labels = required_labels
        self._tool_name = tool_name

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self._tool_name,
            description=(
                f"Query approved structured data from {self._connector.info.display_name}. "
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

        templated = _run_device_status_template(
            self._connector, self._semantics, principal, question
        ) or _run_reviewed_metric_template(self._connector, self._semantics, principal, question)
        if templated is not None:
            if isinstance(templated, ConnectorError):
                return ToolResult(
                    content=str(templated),
                    error=str(templated),
                    metadata={"refused": True},
                )
            record_query(
                self._session,
                principal=principal,
                connector_id=self._connector.info.id,
                sql=templated.sql,
                question=question,
                allowed=True,
                row_count=templated.row_count,
                duration_ms=templated.duration_ms,
            )
            return _render_query_result(
                templated,
                metadata={"template": "approved_reviewed_template"},
            )

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
                metadata={"refused": True, "cost_usd": answer.cost_usd},
            )

        # The SQL is included because it always is — roughly one generated
        # query in five is wrong, and an answer whose query is hidden presents
        # a guess as a fact.
        return _render_query_result(answer.result, metadata={"cost_usd": answer.cost_usd})


_RELATIVE_HOURS = re.compile(r"\b(?:last|past)\s+(\d+)\s+hours?\b", re.IGNORECASE)
_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
_SQL_ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_DEVICE_STATUS = re.compile(r"\b(online|offline)\b", re.IGNORECASE)
_DEVICE_COUNT = re.compile(r"\b(?:how many|count|number of|total)\b", re.IGNORECASE)
_DEVICE_LIST = re.compile(r"\b(?:what|which|list|show)\b.*\bdevices?\b", re.IGNORECASE)


def _run_device_status_template(
    connector: Connector, semantics: Any, principal: Principal, question: str
) -> QueryResult | ConnectorError | None:
    """Answer basic online/offline device questions without a model call."""
    status_match = _DEVICE_STATUS.search(question)
    if status_match is None:
        return None
    # Do not take over a richer question just because it mentions a status.
    #
    # This template answers exactly two things: how many devices have a status,
    # and which ones do. Anything asking for MORE than that — a superlative, a
    # ranking, a time comparison, a metric — must go to the SQL model, which
    # can express it.
    #
    # "Which device has been offline the longest?" matched `_DEVICE_LIST` and
    # `_DEVICE_STATUS` and came back as all five offline devices. The SQL model,
    # asked the same question, wrote `ORDER BY last_seen LIMIT 1` and returned
    # the right one — so the template was not saving a call, it was losing an
    # answer.
    if re.search(
        r"\b(?:temperature|humidity|voltage|pressure|metric|reading|"
        r"average|avg|min(?:imum)?|max(?:imum)?|sum|total|"
        r"longest|shortest|oldest|newest|latest|earliest|first|last|"
        r"most|least|top|bottom|rank|since|before|after|between|"
        r"when|how long|duration)\b",
        question,
        re.I,
    ):
        return None
    # Matched by COLUMNS, not by view name. `.v_devices` is a naming convention
    # of one customer's curated views; a second system with an equivalent view
    # called `equipment` or `v_assets` would carry the same three columns and
    # be refused for its name alone.
    devices_view = next(
        (
            view.name
            for view in getattr(semantics, "views", ())
            if {column.name for column in view.columns} >= {"device_id", "device_name", "status"}
        ),
        None,
    )
    if devices_view is None or not _SQL_IDENTIFIER.fullmatch(devices_view):
        return None
    status = status_match.group(1).lower()
    if _DEVICE_COUNT.search(question):
        sql = f"SELECT COUNT(*) AS {status}_devices FROM {devices_view} WHERE status = '{status}'"  # noqa: S608
    elif _DEVICE_LIST.search(question):
        sql = (  # noqa: S608
            "SELECT device_id, device_name, status "  # noqa: S608
            f"FROM {devices_view} WHERE status = '{status}' ORDER BY device_name"  # noqa: S608
        )
    else:
        return None
    try:
        return connector.query(principal, sql)  # type: ignore[attr-defined, no-any-return]
    except ConnectorError as exc:
        return exc


def _run_reviewed_metric_template(
    connector: Connector, semantics: Any, principal: Principal, question: str
) -> QueryResult | ConnectorError | None:
    """Run a reviewed aggregate without a model-generated SQL step."""
    lowered = question.lower()
    operation = (
        ("AVG", "average")
        if re.search(r"\b(?:average|avg|mean)\b", lowered)
        else ("MIN", "min")
        if re.search(r"\b(?:minimum|min)\b", lowered)
        else ("MAX", "max")
        if re.search(r"\b(?:maximum|max)\b", lowered)
        else None
    )
    if operation is None:
        return None
    for template in getattr(semantics, "reviewed_metric_templates", ()):
        if not any(alias and alias in lowered for alias in template.aliases):
            continue
        # View names came from the connector's metadata discovery. Check their
        # identifier form again so reviewed profile content can never choose a
        # relation or introduce SQL syntax.
        if not (
            _SQL_IDENTIFIER.fullmatch(template.devices_view)
            and _SQL_IDENTIFIER.fullmatch(template.metrics_view)
        ):
            continue
        device = template.device_name.replace("'", "''")
        metric = template.metric.replace("'", "''")
        where = f"d.device_name = '{device}' AND dm.metric = '{metric}'"
        if match := _RELATIVE_HOURS.search(question):
            hours = min(int(match.group(1)), 24 * 31)
            where += f" AND dm.event_time >= NOW() - INTERVAL '{hours}' HOUR"
        alias = f"{operation[1]}_{template.metric}".replace("-", "_")
        if not _SQL_ALIAS.fullmatch(alias):
            continue
        # This SQL has only static clauses, a capped integer, discovered view
        # identifiers, and quote-escaped reviewed values. The SQL connector
        # performs its normal read-only/view allow-list validation before it is
        # sent to MariaDB; the dedicated database reader is the final boundary.
        sql = (
            f"SELECT {operation[0]}(dm.value) AS {alias} FROM {template.metrics_view} AS dm "  # noqa: S608
            f"JOIN {template.devices_view} AS d ON dm.device_id = d.device_id WHERE {where}"
        )
        try:
            return connector.query(principal, sql)  # type: ignore[attr-defined, no-any-return]
        except ConnectorError as exc:
            return exc
    return None


def _render_query_result(result: QueryResult, *, metadata: dict[str, Any]) -> ToolResult:
    """Technical details remain exact and expandable in the console."""
    rows = "\n".join(" | ".join(str(value) for value in row) for row in result.rows[:25])
    content = f"SQL: {result.sql}\n\nColumns: {' | '.join(result.columns)}\n{rows}"
    return ToolResult(
        content=content,
        metadata={"row_count": result.row_count, "truncated": result.truncated, **metadata},
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
