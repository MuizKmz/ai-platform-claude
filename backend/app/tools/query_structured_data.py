"""The query_structured_data tool: a question in, rows and the SQL out.

**The generated SQL is always returned.** Published text-to-SQL accuracy on the
BIRD benchmark is roughly 82% against ~93% for human experts, and researchers
have shown that metric itself disagrees with expert judgment a meaningful share
of the time. So roughly one query in five is wrong, and a wrong query does not
announce itself — it returns rows.

That single fact shapes this module. The tool drafts, it does not decide. Showing
the SQL is what lets a person catch the 20%, and hiding it would present a guess
as an answer.

There is deliberately **no retry loop**. A model that re-drafts after an error
tends to converge on SQL that executes rather than SQL that is correct, and each
attempt costs a query against someone's database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.connectors.base import Connector, ConnectorError, QueryResult
from app.connectors.sql.semantic_layer import SemanticLayer
from app.core.security import Principal
from app.llm.base import LLMError, LLMProvider, Message
from app.observability.audit import record_query

# Fenced SQL, or a bare statement if the model skipped the fence.
_SQL_BLOCK = re.compile(r"```(?:sql)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)

SYSTEM_PROMPT = """\
You write PostgreSQL SELECT queries against a curated read-only schema.

## Rules

1. Emit exactly ONE SELECT statement, in a ```sql fenced block, and nothing else.
2. Query only the views described below. Base tables are not accessible and a
   query naming one will be refused by the database.
3. Use the documented column values verbatim. Inventing a status or category
   that does not exist returns zero rows with no error, which is worse than
   failing.
4. Use the documented join paths. Do not invent joins.
5. Follow the metric definitions exactly, including their caveats.
6. If the question cannot be answered from these views, reply with the single
   word: UNANSWERABLE. Do not guess at a schema that is not described.

Never write INSERT, UPDATE, DELETE, DROP, ALTER, GRANT, or any statement that
modifies data. This schema is read-only and such statements are refused.
"""

UNANSWERABLE = "UNANSWERABLE"


@dataclass(frozen=True)
class StructuredAnswer:
    """The result of a natural-language query.

    `sql` is never None on success and is always surfaced. See the module
    docstring for why that is not optional.
    """

    question: str
    sql: str | None
    result: QueryResult | None
    refused: bool
    error: str | None = None


def query_structured_data(
    session: Session,
    *,
    question: str,
    principal: Principal,
    connector: Connector,
    semantics: SemanticLayer,
    llm: LLMProvider,
    trace_id: str | None = None,
) -> StructuredAnswer:
    """Draft SQL from a question, execute it read-only, and audit the attempt."""
    # Authorization first, before a token is spent. A caller who may not use this
    # connector should not be able to make us pay for a generation.
    connector.authorize(principal)

    try:
        drafted = _draft_sql(question, semantics, llm)
    except LLMError:
        record_query(
            session,
            principal=principal,
            connector_id=connector.info.id,
            sql="",
            question=question,
            allowed=False,
            denial_reason="sql generation failed",
            trace_id=trace_id,
        )
        return StructuredAnswer(
            question=question,
            sql=None,
            result=None,
            refused=True,
            error="The query could not be drafted.",
        )

    if drafted is None:
        # The model said the question is unanswerable from this schema. Audited
        # as a refusal, since a rising rate of these is a signal about the
        # semantic layer rather than about users.
        record_query(
            session,
            principal=principal,
            connector_id=connector.info.id,
            sql="",
            question=question,
            allowed=False,
            denial_reason="unanswerable from the available schema",
            trace_id=trace_id,
        )
        return StructuredAnswer(
            question=question,
            sql=None,
            result=None,
            refused=True,
            error="That question cannot be answered from the available data.",
        )

    try:
        result = connector.query(principal, drafted)  # type: ignore[attr-defined]
    except ConnectorError as exc:
        # Covers both validator refusals and database refusals. The drafted SQL
        # is recorded either way — a refused query is the more interesting row.
        record_query(
            session,
            principal=principal,
            connector_id=connector.info.id,
            sql=drafted,
            question=question,
            allowed=False,
            denial_reason=str(exc),
            trace_id=trace_id,
        )
        return StructuredAnswer(
            question=question, sql=drafted, result=None, refused=True, error=str(exc)
        )

    record_query(
        session,
        principal=principal,
        connector_id=connector.info.id,
        sql=result.sql,
        question=question,
        allowed=True,
        row_count=result.row_count,
        duration_ms=result.duration_ms,
        trace_id=trace_id,
    )
    return StructuredAnswer(question=question, sql=result.sql, result=result, refused=False)


def _draft_sql(question: str, semantics: SemanticLayer, llm: LLMProvider) -> str | None:
    """Generate one SELECT, or None if the model declines.

    Returns None rather than raising for UNANSWERABLE: a model correctly saying
    "not from this schema" is a success of the design, not an error condition.
    """
    messages = [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(
            role="user",
            content=f"{semantics.to_prompt()}\n\n## Question\n\n{question}",
        ),
    ]
    completion = llm.complete(messages, max_tokens=600)
    text = completion.text.strip()

    # A model told to reply UNANSWERABLE sometimes complies *in SQL*, e.g.
    # `SELECT 'UNANSWERABLE' AS answer`. That executes and returns a row, so a
    # caller would see a result set where a refusal was intended. Treat the
    # marker as a refusal wherever it appears, unless it is merely a value being
    # compared against — which no legitimate query over this schema does.
    if UNANSWERABLE in text.upper():
        return None

    match = _SQL_BLOCK.search(text)
    sql = match.group(1).strip() if match else text

    # A model that ignored the fencing instruction may have added prose. Keep
    # from the first SELECT or WITH; the validator rejects whatever survives if
    # this heuristic guessed wrong.
    lowered = sql.lower()
    start = min(
        (pos for pos in (lowered.find("select"), lowered.find("with")) if pos != -1),
        default=-1,
    )
    if start > 0:
        sql = sql[start:]

    return sql.rstrip().rstrip(";") or None
