"""The query_structured_data tool: audit, refusal, and what reaches the caller.

Generation quality is not asserted here — that is what the accuracy eval
measures, with a real model and an honest number. These tests cover the plumbing
around it, which must behave identically whatever the model says today.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorAuthorizationError
from app.connectors.egress import EgressPolicy, ResolvedTarget
from app.connectors.sql.connector import SQLConnector, SQLConnectorConfig
from app.connectors.sql.semantic_layer import ANALYTICS_SEMANTICS
from app.core.config import settings
from app.core.security import Principal
from app.llm.providers.fake import FakeLLM
from app.tools.query_structured_data import query_structured_data

pytestmark = pytest.mark.security

TENANT = uuid.UUID("5e5e0000-0000-0000-0000-00000000005e")


def _analytics_available() -> bool:
    try:
        engine = create_engine(
            f"postgresql+psycopg://analytics_readonly:{settings.postgres_readonly_password}"
            f"@{settings.postgres_host}:{settings.postgres_port}/analytics",
            connect_args={"connect_timeout": 3},
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


requires_analytics = pytest.mark.skipif(
    not _analytics_available(), reason="analytics database not provisioned"
)

ANALYST = Principal(
    tenant_id=TENANT,
    user_id=uuid.uuid4(),
    email="analyst@test",
    roles=("reader",),
    allowed_labels=("analytics",),
)

OUTSIDER = Principal(
    tenant_id=TENANT,
    user_id=uuid.uuid4(),
    email="outsider@test",
    roles=("reader",),
    allowed_labels=("public",),
)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def tenant_row(engine: Engine) -> Iterator[None]:
    def _wipe() -> None:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM connector_audit WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": TENANT})

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenant (id, slug, name) VALUES (:t, 'tool-test', 'Tool')"),
            {"t": TENANT},
        )
    yield
    _wipe()


@pytest.fixture(scope="module")
def connector() -> Iterator[SQLConnector]:
    config = SQLConnectorConfig(
        id="analytics",
        display_name="Analytics",
        host=settings.postgres_host,
        port=settings.postgres_port,
        database="analytics",
        username="analytics_readonly",
        password=SecretStr(settings.postgres_readonly_password),
        required_labels=("analytics",),
        egress=EgressPolicy(allow_private=True),
    )
    with patch(
        "app.connectors.sql.connector.resolve_and_validate",
        return_value=ResolvedTarget(host=config.host, ip="127.0.0.1", port=config.port),
    ):
        conn = SQLConnector(config)
    yield conn
    conn.dispose()


def _ask(
    engine: Engine, connector: SQLConnector, llm: FakeLLM, question: str, principal: Principal
) -> object:
    with Session(engine) as session, session.begin():
        return query_structured_data(
            session,
            question=question,
            principal=principal,
            connector=connector,
            semantics=ANALYTICS_SEMANTICS,
            llm=llm,
        )


def _audit_rows(engine: Engine) -> list:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT * FROM connector_audit WHERE tenant_id = :t ORDER BY created_at"),
            {"t": TENANT},
        ).all()


# --- authorization ------------------------------------------------------------


@requires_analytics
def test_unauthorized_principal_is_refused_before_any_generation(
    engine: Engine, connector: SQLConnector
) -> None:
    """A caller who may not use the connector must not make us pay for tokens."""
    llm = FakeLLM(response="```sql\nSELECT 1\n```")

    with pytest.raises(ConnectorAuthorizationError):
        _ask(engine, connector, llm, "How many orders?", OUTSIDER)

    assert llm.calls == [], "the model was called for an unauthorized principal"


# --- audit --------------------------------------------------------------------


@requires_analytics
def test_sql_audit_written_on_success(engine: Engine, connector: SQLConnector) -> None:
    llm = FakeLLM(response="```sql\nSELECT count(*) FROM curated.v_orders\n```")

    _ask(engine, connector, llm, "How many orders?", ANALYST)

    rows = _audit_rows(engine)
    assert len(rows) == 1
    assert rows[0].allowed is True
    assert rows[0].question == "How many orders?"
    assert "curated.v_orders" in rows[0].sql
    assert rows[0].row_count == 1
    assert rows[0].user_email == "analyst@test"


@requires_analytics
def test_denied_queries_are_audited_too(engine: Engine, connector: SQLConnector) -> None:
    """The more valuable rows. A run of refusals is what a bypass attempt looks
    like, and a log of successes alone cannot show it."""
    llm = FakeLLM(response="```sql\nDELETE FROM curated.v_orders\n```")

    answer = _ask(engine, connector, llm, "Remove all orders", ANALYST)

    assert answer.refused is True  # type: ignore[attr-defined]
    rows = _audit_rows(engine)
    assert len(rows) == 1
    assert rows[0].allowed is False
    assert rows[0].denial_reason
    # The refused SQL is recorded, not discarded.
    assert "DELETE" in rows[0].sql.upper()


@requires_analytics
def test_base_table_access_is_audited_as_denied(engine: Engine, connector: SQLConnector) -> None:
    """Refused by the database rather than the validator, and still audited."""
    llm = FakeLLM(response="```sql\nSELECT * FROM public.customers\n```")

    answer = _ask(engine, connector, llm, "Show raw customers", ANALYST)

    assert answer.refused is True  # type: ignore[attr-defined]
    rows = _audit_rows(engine)
    assert rows[0].allowed is False


@requires_analytics
def test_unanswerable_questions_are_audited(engine: Engine, connector: SQLConnector) -> None:
    """A rising rate of these is a signal about the semantic layer, not users."""
    llm = FakeLLM(response="UNANSWERABLE")

    answer = _ask(engine, connector, llm, "What is the CEO's salary?", ANALYST)

    assert answer.refused is True  # type: ignore[attr-defined]
    assert answer.sql is None  # type: ignore[attr-defined]
    rows = _audit_rows(engine)
    assert rows[0].allowed is False
    assert "unanswerable" in rows[0].denial_reason.lower()


# --- what reaches the caller --------------------------------------------------


@requires_analytics
def test_generated_sql_is_always_returned(engine: Engine, connector: SQLConnector) -> None:
    """Roughly one text-to-SQL query in five is wrong, and a wrong one returns
    rows rather than an error. Showing the SQL is what lets a person catch it."""
    llm = FakeLLM(response="```sql\nSELECT count(*) FROM curated.v_orders\n```")

    answer = _ask(engine, connector, llm, "How many orders?", ANALYST)

    assert answer.sql is not None  # type: ignore[attr-defined]
    assert "curated.v_orders" in answer.sql  # type: ignore[attr-defined]


@requires_analytics
def test_refused_sql_is_returned_so_it_can_be_inspected(
    engine: Engine, connector: SQLConnector
) -> None:
    llm = FakeLLM(response="```sql\nDROP VIEW curated.v_orders\n```")

    answer = _ask(engine, connector, llm, "Delete the orders view", ANALYST)

    assert answer.refused is True  # type: ignore[attr-defined]
    assert "DROP" in (answer.sql or "").upper()  # type: ignore[attr-defined]


@requires_analytics
def test_a_limit_is_always_applied(engine: Engine, connector: SQLConnector) -> None:
    llm = FakeLLM(response="```sql\nSELECT * FROM curated.v_orders\n```")

    answer = _ask(engine, connector, llm, "Show every order", ANALYST)

    assert "LIMIT" in (answer.sql or "").upper()  # type: ignore[attr-defined]


# --- prompt construction ------------------------------------------------------


@requires_analytics
def test_the_semantic_layer_reaches_the_model(engine: Engine, connector: SQLConnector) -> None:
    """Enumerated values are the highest-value part: without them a model guesses
    'complete' for a status whose real values are pending/shipped/cancelled, and
    returns zero rows with no error."""
    llm = FakeLLM(response="```sql\nSELECT 1\n```")

    _ask(engine, connector, llm, "How many orders?", ANALYST)

    prompt = "".join(m.content for m in llm.calls[0])
    assert "curated.v_orders" in prompt
    assert "'shipped'" in prompt
    assert "Example queries" in prompt
    # The caveat that changes an answer's meaning.
    assert "cancelled orders are INCLUDED".lower() in prompt.lower()


@requires_analytics
def test_prose_around_the_sql_is_tolerated(engine: Engine, connector: SQLConnector) -> None:
    """Models add commentary. The statement is still extracted."""
    llm = FakeLLM(
        response="Here is the query you asked for:\n\n"
        "```sql\nSELECT count(*) FROM curated.v_orders\n```\n\nHope that helps."
    )

    answer = _ask(engine, connector, llm, "How many orders?", ANALYST)

    assert answer.refused is False  # type: ignore[attr-defined]
    assert "Hope that helps" not in (answer.sql or "")  # type: ignore[attr-defined]


@requires_analytics
def test_generation_failure_is_handled_and_audited(engine: Engine, connector: SQLConnector) -> None:
    llm = FakeLLM(raise_error=True)

    answer = _ask(engine, connector, llm, "How many orders?", ANALYST)

    assert answer.refused is True  # type: ignore[attr-defined]
    assert "fake failure" not in (answer.error or "")  # type: ignore[attr-defined]
    assert _audit_rows(engine)[0].allowed is False


@requires_analytics
def test_unanswerable_expressed_as_sql_is_still_a_refusal(
    engine: Engine, connector: SQLConnector
) -> None:
    """A model told to reply UNANSWERABLE sometimes complies in SQL.

    `SELECT 'UNANSWERABLE' AS answer` executes and returns a row, so a caller
    would see a result set where a refusal was intended. Found by the accuracy
    eval, not by reading the code.
    """
    llm = FakeLLM(response="```sql\nSELECT 'UNANSWERABLE' AS profit_margin\n```")

    answer = _ask(engine, connector, llm, "What is the profit margin?", ANALYST)

    assert answer.refused is True  # type: ignore[attr-defined]
    assert answer.result is None  # type: ignore[attr-defined]
