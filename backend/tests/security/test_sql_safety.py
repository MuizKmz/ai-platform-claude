"""SQL safety, from the parser down to the database role.

The test that matters most here is `test_write_rejected_at_db_level_too`. It skips
the validator entirely and issues a write straight to the connection. If that
passes, every other test in this file is decoration — they would all be checking
a filter in front of a door that was never locked.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text

from app.connectors.base import ConnectorAuthorizationError, ConnectorError
from app.connectors.egress import EgressPolicy
from app.connectors.sql.connector import SQLConnector, SQLConnectorConfig
from app.connectors.sql.safety import MAX_ROWS, UnsafeSQLError, validate
from app.core.config import settings
from app.core.security import Principal

pytestmark = pytest.mark.security

ANALYTICS_DB = "analytics"
READONLY_USER = "analytics_readonly"


def _analytics_available() -> bool:
    try:
        engine = create_engine(_readonly_url(), connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


def _readonly_url() -> str:
    return (
        f"postgresql+psycopg://{READONLY_USER}:{settings.postgres_readonly_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{ANALYTICS_DB}"
    )


requires_analytics = pytest.mark.skipif(
    not _analytics_available(),
    reason="analytics database not provisioned",
)

PRINCIPAL = Principal(
    tenant_id=uuid.uuid4(),
    user_id=uuid.uuid4(),
    email="analyst@test",
    roles=("reader",),
    allowed_labels=("analytics",),
)


@pytest.fixture(scope="module")
def connector() -> Iterator[SQLConnector]:
    config = SQLConnectorConfig(
        id="analytics",
        display_name="Analytics warehouse",
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=ANALYTICS_DB,
        username=READONLY_USER,
        password=SecretStr(settings.postgres_readonly_password),
        required_labels=("analytics",),
        # The test database is on localhost, which egress control blocks by
        # design. Permitted explicitly here so the test exercises SQL safety
        # rather than re-testing egress, which has its own suite.
        egress=EgressPolicy(allow_private=True, allowed_hosts=frozenset()),
    )
    # Loopback is blocked unconditionally, so resolve against the container IP.
    object.__setattr__(config, "host", settings.postgres_host)
    conn = _build(config)
    yield conn
    conn.dispose()


def _build(config: SQLConnectorConfig) -> SQLConnector:
    """Construct a connector, bypassing egress for the local test database."""
    from unittest.mock import patch

    from app.connectors.egress import ResolvedTarget

    with patch(
        "app.connectors.sql.connector.resolve_and_validate",
        return_value=ResolvedTarget(host=config.host, ip="127.0.0.1", port=config.port),
    ):
        return SQLConnector(config)


@pytest.fixture(scope="module")
def raw_readonly_engine() -> Iterator[Engine]:
    """A connection as the read-only role, with NO validator in front of it."""
    engine = create_engine(_readonly_url())
    yield engine
    engine.dispose()


# --- layer 4: the AST validator (no database needed) --------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO curated.v_orders VALUES (1)",
        "UPDATE curated.v_orders SET order_status = 'x'",
        "DELETE FROM curated.v_orders",
        "DROP VIEW curated.v_orders",
        "ALTER VIEW curated.v_orders RENAME TO x",
        "TRUNCATE TABLE orders",
        "GRANT SELECT ON curated.v_orders TO public",
        "COPY curated.v_orders TO '/tmp/out.csv'",
    ],
)
def test_write_statement_rejected(statement: str) -> None:
    with pytest.raises(UnsafeSQLError):
        validate(statement)


def test_cte_write_rejected() -> None:
    """A SELECT at the top level that deletes rows underneath.

    Checking only the root node would pass this, which is why write nodes are
    rejected anywhere in the tree.
    """
    with pytest.raises(UnsafeSQLError, match="DELETE is not permitted"):
        validate("WITH removed AS (DELETE FROM orders RETURNING *) SELECT * FROM removed")


def test_stacked_statement_rejected() -> None:
    with pytest.raises(UnsafeSQLError, match="one statement"):
        validate("SELECT 1; DROP TABLE orders")


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_sleep(30)",
        "SELECT dblink('host=evil', 'SELECT 1')",
        "SELECT lo_import('/etc/shadow')",
        "SELECT pg_terminate_backend(1)",
    ],
)
def test_system_function_rejected(statement: str) -> None:
    with pytest.raises(UnsafeSQLError, match="not permitted"):
        validate(statement)


def test_limit_is_injected_when_absent() -> None:
    """A missing LIMIT is the difference between an answer and an export."""
    result = validate("SELECT * FROM curated.v_orders")

    assert f"LIMIT {MAX_ROWS}" in result.sql.upper()
    assert result.limit_applied is True


def test_oversized_limit_is_lowered() -> None:
    result = validate("SELECT * FROM curated.v_orders LIMIT 999999")

    assert "999999" not in result.sql
    assert result.limit_applied is True


def test_mysql_last_24_hours_interval_is_permitted() -> None:
    """MySQL parses the HOUR unit as exp.Var; permit that narrow safe form."""
    result = validate(
        "SELECT AVG(value) FROM curated.v_metrics WHERE event_time >= NOW() - INTERVAL 24 HOUR",
        dialect="mysql",
    )

    assert "INTERVAL '24' HOUR" in result.sql.upper()


def test_unsupported_mysql_interval_unit_remains_rejected() -> None:
    with pytest.raises(UnsafeSQLError, match="Var is not an allowed"):
        validate(
            "SELECT AVG(value) FROM curated.v_metrics "
            "WHERE event_time >= NOW() - INTERVAL 24 FORTNIGHT",
            dialect="mysql",
        )


def test_reasonable_limit_is_left_alone() -> None:
    result = validate("SELECT * FROM curated.v_orders LIMIT 10")

    assert "LIMIT 10" in result.sql
    assert result.limit_applied is False


def test_ordinary_analytical_queries_pass() -> None:
    """The allowlist must not be so tight that real questions fail."""
    for statement in (
        "SELECT count(*) FROM curated.v_orders",
        "SELECT customer_country, count(*) FROM curated.v_orders GROUP BY customer_country",
        "SELECT * FROM curated.v_orders WHERE ordered_at > '2026-01-01' ORDER BY ordered_at DESC",
        "SELECT o.customer_name, sum(l.line_total) FROM curated.v_orders o "
        "JOIN curated.v_order_lines l ON l.order_id = o.order_id GROUP BY o.customer_name",
        "WITH recent AS (SELECT * FROM curated.v_orders WHERE ordered_at > '2026-06-01') "
        "SELECT count(*) FROM recent",
    ):
        validate(statement)


# --- layer 1: the database itself ---------------------------------------------


@requires_analytics
@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO curated.v_customers (customer_name) VALUES ('evil')",
        "UPDATE curated.v_customers SET customer_name = 'evil'",
        "DELETE FROM curated.v_customers",
        "DROP VIEW curated.v_orders",
        "CREATE TABLE mischief (id int)",
        "TRUNCATE TABLE public.orders",
    ],
)
def test_write_rejected_at_db_level_too(raw_readonly_engine: Engine, statement: str) -> None:
    """THE test. The validator is bypassed entirely and the database still refuses.

    If this ever passes, every other test in this file is decoration: they would
    be checking a filter in front of a door that was never locked.
    """
    with pytest.raises(Exception) as exc, raw_readonly_engine.connect() as conn:
        conn.execute(text(statement))

    message = str(exc.value).lower()
    assert "permission denied" in message or "read-only" in message


@requires_analytics
def test_base_tables_are_unreachable(raw_readonly_engine: Engine) -> None:
    """The role has rights to curated views only.

    A query naming a base table fails at the database, so the curated layer is a
    boundary rather than a convention.
    """
    with (
        pytest.raises(Exception, match="(?i)permission denied"),
        raw_readonly_engine.connect() as conn,
    ):
        conn.execute(text("SELECT * FROM public.customers"))


@requires_analytics
def test_role_is_not_superuser(raw_readonly_engine: Engine) -> None:
    """Everything above rests on this. A superuser bypasses grants entirely."""
    with raw_readonly_engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()

    assert row.rolsuper is False
    assert row.rolbypassrls is False


@requires_analytics
def test_session_defaults_to_read_only(raw_readonly_engine: Engine) -> None:
    """Layer 2: set on the ROLE, so a connection that forgets to ask still gets it.

    This is the layer MySQL has no equivalent of — worth knowing before Phase 5.
    """
    with raw_readonly_engine.connect() as conn:
        value = conn.execute(text("SHOW default_transaction_read_only")).scalar()

    assert value == "on"


# --- layer 3: timeouts and row caps -------------------------------------------


@requires_analytics
def test_statement_timeout_enforced(raw_readonly_engine: Engine) -> None:
    """A slow query is killed rather than held.

    pg_sleep is blocked by the validator, so this goes direct — the point is that
    the timeout is enforced by the database, not by the parser refusing sleeps.
    """
    with pytest.raises(Exception) as exc, raw_readonly_engine.connect() as conn:
        conn.execute(text("SET statement_timeout = '200ms'"))
        conn.execute(text("SELECT pg_sleep(5)"))

    assert "timeout" in str(exc.value).lower()


# --- the connector as a whole -------------------------------------------------


@requires_analytics
def test_connector_executes_a_real_query(connector: SQLConnector) -> None:
    result = connector.query(PRINCIPAL, "SELECT customer_name FROM curated.v_customers")

    assert result.row_count > 0
    assert "customer_name" in result.columns
    # The executed SQL is returned, not the SQL that was submitted: published
    # text-to-SQL accuracy is ~82%, so a caller needs to see what actually ran.
    assert "LIMIT" in result.sql.upper()


@requires_analytics
def test_connector_refuses_a_write(connector: SQLConnector) -> None:
    with pytest.raises(ConnectorError):
        connector.query(PRINCIPAL, "DELETE FROM curated.v_orders")


@requires_analytics
def test_connector_requires_an_authorized_principal(connector: SQLConnector) -> None:
    """A caller without the connector's label cannot query it at all."""
    outsider = Principal(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        email="outsider@test",
        roles=("reader",),
        allowed_labels=("public",),
    )

    with pytest.raises(ConnectorAuthorizationError):
        connector.query(outsider, "SELECT 1")


@requires_analytics
def test_upstream_errors_are_not_echoed(connector: SQLConnector) -> None:
    """Database errors name schemas, roles, and columns. None of that reaches a caller."""
    with pytest.raises(ConnectorError) as exc:
        connector.query(PRINCIPAL, "SELECT * FROM public.customers")

    message = str(exc.value)
    assert "public.customers" not in message
    assert READONLY_USER not in message


@requires_analytics
def test_schema_discovery_shows_only_curated_views(connector: SQLConnector) -> None:
    """information_schema is privilege-filtered, so this cannot describe a table
    the connector could not query."""
    tables = connector.describe_schema(PRINCIPAL)

    names = {t["name"] for t in tables}
    assert names
    assert all(name.startswith("curated.") for name in names)


@requires_analytics
def test_credentials_never_appear_in_the_connector_repr(connector: SQLConnector) -> None:
    """A connector object reaches log lines and exception context."""
    assert settings.postgres_readonly_password not in repr(connector)
