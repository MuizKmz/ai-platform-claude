"""SQL safety against MySQL — the engine the target systems actually run.

Every layer in `sql/safety.py` was built and tested against PostgreSQL. The ERP,
WMS, MES, and IoT systems this platform exists to serve all run MySQL, so the
claim *"the database refuses writes"* was proven for an engine nobody uses.

This file holds MySQL to the same standard, and the test that matters most is
the same one:

    test_write_rejected_at_db_level_too

It bypasses the AST validator entirely and hands a write straight to the
connection. If it ever passes, every other SQL safety test for MySQL is
decoration — exactly as CLAUDE.md says of the Postgres original.

**Why a real MySQL rather than a mock.** The question these tests ask is
"does the GRANT refuse this?", and a mock is the wrong thing to ask. It would
answer whatever the test wanted.

Skipped when the container is absent, because a skipped test that would have
passed is better than a green one that never ran — and CI's security guard
fails the build if any security test skips, which is what keeps that honest.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text

from app.connectors.base import ConnectorError
from app.connectors.egress import EgressPolicy
from app.connectors.sql.connector import SQLConnector, SQLConnectorConfig
from app.connectors.sql.safety import UnsafeSQLError, validate
from app.core.security import Principal

# Read from the environment rather than `settings`: MySQL is a test fixture, not
# a configured dependency of the application. Nothing in `app/` connects to it —
# a MySQL connector is created at runtime from a row in the `connector` table,
# with its own host and port. Adding a field to Settings would imply the
# application has one MySQL, which it does not.
#
# Two sources, in the order that makes each correct:
#
#   1. `MYSQL_PORT` in the environment. This is what CI sets (3306, where
#      nothing competes for it).
#   2. `MYSQL_PORT` in `.env`, read directly. Docker Compose reads that file to
#      decide the published port, and pydantic-settings does NOT export it to
#      `os.environ` — so without this the tests would use a hardcoded default
#      while compose published something else, and every MySQL test would skip
#      with the container running and healthy.
#
# The bare default is last and matches `.env.example`.


def _mysql_port() -> int:
    from_env = os.environ.get("MYSQL_PORT")
    if from_env:
        return int(from_env)

    dotenv = pathlib.Path(__file__).resolve().parents[3] / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "MYSQL_PORT" and value.strip():
                return int(value.split("#")[0].strip())
    return 3307


MYSQL_PORT = _mysql_port()
MYSQL_URL = (
    f"mysql+pymysql://analytics_readonly:ci-readonly-password@127.0.0.1:{MYSQL_PORT}/curated"
)


def _mysql_available() -> bool:
    try:
        engine = create_engine(MYSQL_URL, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


pytestmark = [
    pytest.mark.security,
    pytest.mark.skipif(not _mysql_available(), reason="MySQL container not running"),
]

ANALYST = Principal(
    tenant_id=uuid.UUID("5a5a0000-0000-0000-0000-00000000005a"),
    user_id=uuid.uuid4(),
    email="analyst@test",
    roles=("reader",),
    allowed_labels=("analytics",),
)


@pytest.fixture(scope="module")
def raw_readonly_engine() -> Iterator[Engine]:
    """The read-only user, with NO application code in the way.

    This is what makes the write tests meaningful: they go straight to MySQL,
    so what they prove is what MySQL permits — not what our validator happens
    to catch.
    """
    engine = create_engine(MYSQL_URL)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def connector() -> Iterator[SQLConnector]:
    config = SQLConnectorConfig(
        id="mysql-analytics",
        display_name="MySQL analytics",
        host="127.0.0.1",
        port=MYSQL_PORT,
        database="curated",
        username="analytics_readonly",
        password=SecretStr("ci-readonly-password"),
        schema="curated",
        required_labels=("analytics",),
        egress=EgressPolicy(allow_private=True, allow_loopback=True),
        engine="mysql",
    )
    built = SQLConnector(config)
    yield built


# --- THE test ------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO curated.v_orders (order_id) VALUES (9999)",
        "UPDATE curated.v_orders SET order_status = 'shipped'",
        "DELETE FROM curated.v_orders",
        "CREATE TABLE evil (id INT)",
        "DROP VIEW curated.v_orders",
        "TRUNCATE TABLE curated.v_orders",
    ],
)
def test_write_rejected_at_db_level_too(raw_readonly_engine: Engine, statement: str) -> None:
    """The grant refuses every write, with the validator bypassed entirely.

    Postgres enforces this with a role that has SELECT and
    `default_transaction_read_only`. MySQL has no such role flag — SELECT and
    nothing else IS the guarantee, plus the session setting the connector
    applies on connect.

    If this test ever passes, the MySQL connector is unsafe regardless of what
    the AST validator does.
    """
    from sqlalchemy.exc import DatabaseError

    with pytest.raises(DatabaseError), raw_readonly_engine.begin() as conn:
        conn.execute(text(statement))


def test_base_tables_are_unreachable(raw_readonly_engine: Engine) -> None:
    """Curated views only. The base tables hold columns the views omit.

    `analytics.customers` carries a `credit_card` column that
    `curated.v_customers` deliberately does not select. If the base table were
    readable, the curated layer would be decoration.
    """
    from sqlalchemy.exc import DatabaseError

    for table in ("analytics.customers", "analytics.orders", "analytics.order_lines"):
        with pytest.raises(DatabaseError), raw_readonly_engine.connect() as conn:
            conn.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608


def test_the_omitted_column_is_genuinely_unreachable(raw_readonly_engine: Engine) -> None:
    """Named separately because it is the point of a curated view.

    A view that omits a column is only a control if the column cannot be
    reached another way.
    """
    from sqlalchemy.exc import DatabaseError

    with pytest.raises(DatabaseError), raw_readonly_engine.connect() as conn:
        conn.execute(text("SELECT credit_card FROM analytics.customers"))


def test_system_tables_are_unreachable(raw_readonly_engine: Engine) -> None:
    """`mysql.user` is MySQL's equivalent of the `pg_user` hole the red-team
    corpus found in Phase 8 — readable by default in Postgres, and worth
    checking rather than assuming here."""
    from sqlalchemy.exc import DatabaseError

    with pytest.raises(DatabaseError), raw_readonly_engine.connect() as conn:
        conn.execute(text("SELECT count(*) FROM mysql.user"))


# --- the session settings ------------------------------------------------------


def test_every_connection_starts_read_only(connector: SQLConnector) -> None:
    """MySQL has no `default_transaction_read_only`, so the connector sets it.

    Applied by a `connect` event rather than once after create_engine: the pool
    opens connections lazily and replaces dropped ones, and a setting applied
    once would cover the first connection and silently miss the rest.
    """
    with connector._engine.connect() as conn:  # noqa: SLF001
        assert conn.execute(text("SELECT @@session.transaction_read_only")).scalar() == 1


def test_every_connection_is_time_bounded(connector: SQLConnector) -> None:
    """A wrong query should be slow-and-killed rather than an outage."""
    with connector._engine.connect() as conn:  # noqa: SLF001
        limit = conn.execute(text("SELECT @@session.max_execution_time")).scalar()
    assert limit and int(limit) > 0


def test_a_second_connection_also_starts_read_only(connector: SQLConnector) -> None:
    """The reason the setting is an event.

    Opens two connections at once so the pool must create a second, and checks
    it carries the setting too.
    """
    with connector._engine.connect() as first, connector._engine.connect() as second:  # noqa: SLF001
        for conn in (first, second):
            assert conn.execute(text("SELECT @@session.transaction_read_only")).scalar() == 1


# --- the validator, in MySQL dialect -------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TABLE customers",
        "DELETE FROM orders",
        "UPDATE orders SET order_status = 'shipped'",
        "INSERT INTO customers (customer_name) VALUES ('x')",
        "TRUNCATE customers",
        "SELECT 1; DROP TABLE customers",
        "SELECT * FROM analytics.customers",
        "SELECT * FROM mysql.user",
        "SELECT load_file('/etc/passwd')",
        "SELECT sleep(100)",
        "SELECT * FROM curated.v_orders UNION SELECT * FROM mysql.user",
        "GRANT ALL ON curated.* TO 'analytics_readonly'@'%'",
    ],
)
def test_the_validator_refuses_mysql_escapes(statement: str) -> None:
    """The same corpus the Postgres validator refuses, parsed as MySQL.

    The allowlist is shared between dialects deliberately: a construction
    permitted for one is permitted for both, so a statement cannot be safe in
    Postgres and unexamined in MySQL.
    """
    with pytest.raises(UnsafeSQLError):
        validate(statement, dialect="mysql", schema="curated")


def test_ordinary_analytical_queries_pass() -> None:
    """The complement. A validator that refused everything would satisfy every
    test above and be useless."""
    for statement in (
        "SELECT count(*) FROM curated.v_orders",
        "SELECT customer_name, sum(total_amount) FROM curated.v_orders GROUP BY customer_name",
        "SELECT * FROM curated.v_orders WHERE order_status = 'shipped' ORDER BY order_date DESC",
    ):
        result = validate(statement, dialect="mysql", schema="curated")
        assert "LIMIT" in result.sql.upper()


# --- the connector end to end --------------------------------------------------


def test_the_connector_executes_a_real_query(connector: SQLConnector) -> None:
    """Against a real MySQL, through every layer."""
    result = connector.query(ANALYST, "SELECT count(*) AS n FROM curated.v_orders")
    assert result.columns == ("n",)
    assert int(result.rows[0][0]) == 5
    # The executed SQL is surfaced, not the SQL that was asked for: the safety
    # layer rewrote it, and showing the original would misrepresent what ran.
    assert "LIMIT" in result.sql.upper()


def test_the_connector_refuses_a_write(connector: SQLConnector) -> None:
    """Refused by the validator, before the database is asked."""
    with pytest.raises((UnsafeSQLError, ConnectorError)):
        connector.query(ANALYST, "DELETE FROM curated.v_orders")


def test_the_connector_caps_rows(connector: SQLConnector) -> None:
    """A question must not become an export."""
    result = connector.query(ANALYST, "SELECT * FROM curated.v_order_lines")
    assert len(result.rows) <= 1000


def test_schema_discovery_shows_only_curated_views(connector: SQLConnector) -> None:
    """What the model is told exists. Naming a base table here would invite it
    to write SQL that is refused — a worse experience than not knowing.

    `describe_schema` reads `information_schema.columns`, and the docstring on
    that method says the result is filtered by privilege. That is true of
    Postgres. MySQL filters `information_schema` too, but on a different rule —
    a row appears if the user holds *any* privilege on the object — so the
    guarantee is worth testing rather than inheriting.
    """
    described = connector.describe_schema(ANALYST)
    names = {table["name"].lower() for table in described}

    assert names, "the connector described nothing"
    assert names == {"curated.v_customers", "curated.v_order_lines", "curated.v_orders"}, (
        f"unexpected objects described to the model: {sorted(names)}"
    )


def test_schema_discovery_omits_the_sensitive_column(connector: SQLConnector) -> None:
    """The credit-card column is absent from the curated view, and so must be
    absent from what the model is shown. A model that knows a column exists
    will write SQL that asks for it."""
    described = connector.describe_schema(ANALYST)
    columns = {column["name"].lower() for table in described for column in table["columns"]}
    assert "credit_card" not in columns, "a redacted column was described to the model"


def test_credentials_never_appear_in_the_connector_repr(connector: SQLConnector) -> None:
    """A repr lands in a log or a traceback, and a password in either is a
    password in a place nobody audits."""
    text_form = f"{connector!r} {connector._config!r}"  # noqa: SLF001
    assert "ci-readonly-password" not in text_form


# --- reaching MySQL through the control plane ----------------------------------
#
# The tests above build a connector directly. That proves the connector works
# and proves nothing about whether an operator can actually create one, which is
# the only way a MySQL source exists in production: a row in the `connector`
# table, written through the integrations API.
#
# `SQLSettings.engine` did not exist until this file was written. Every
# connector built from a stored row defaulted to postgres, so a MySQL
# integration was unreachable from the console no matter what was typed into it.


def test_a_stored_row_can_specify_mysql() -> None:
    """The engine survives the trip through the settings model.

    Built through `build_connector`, the same function the API calls, rather
    than by constructing SQLConnectorConfig directly — the gap this closes was
    precisely between those two paths.
    """
    from app.connectors.registry_store import build_connector

    built = build_connector(
        kind="sql",
        slug="mysql-via-registry",
        display_name="MySQL via registry",
        settings={
            "engine": "mysql",
            "host": "127.0.0.1",
            "port": MYSQL_PORT,
            "database": "curated",
            "username": "analytics_readonly",
            "schema_name": "curated",
            "allow_private": True,
            "allow_loopback": True,
        },
        credential=SecretStr("ci-readonly-password"),
        required_labels=("analytics",),
    )
    try:
        assert built.health() is True, "a MySQL connector built from a stored row cannot connect"
        result = built.query(ANALYST, "SELECT count(*) AS n FROM curated.v_orders")
        assert int(result.rows[0][0]) == 5
    finally:
        built.dispose()


def test_a_row_that_predates_mysql_support_still_means_postgres() -> None:
    """Absent field, unchanged meaning.

    Every connector stored before this change has no `engine` key. If the
    default were anything but postgres, adding MySQL support would have
    silently repointed every existing integration at the wrong driver.
    """
    from app.connectors.registry_store import SQLSettings

    parsed = SQLSettings.model_validate(
        {
            "kind": "sql",
            "host": "db.internal",
            "port": 5432,
            "database": "analytics",
            "username": "readonly",
        }
    )
    assert parsed.engine == "postgres"


def test_an_engine_the_validator_cannot_parse_is_refused_at_configuration() -> None:
    """Fail when the row is written, not when a query is run.

    `safety.validate` refuses an unsupported dialect, so an unconstrained field
    would still be safe — but the operator would see it as a confusing
    per-query refusal long after saving the integration, with nothing pointing
    at the typo.
    """
    import pydantic

    from app.connectors.registry_store import SQLSettings

    with pytest.raises(pydantic.ValidationError):
        SQLSettings.model_validate(
            {
                "kind": "sql",
                "engine": "oracle",
                "host": "db.internal",
                "port": 1521,
                "database": "analytics",
                "username": "readonly",
            }
        )


# --- MariaDB ------------------------------------------------------------------
#
# MariaDB forked from MySQL before 5.7.8 and never adopted
# `max_execution_time`; it has `max_statement_time`, in SECONDS rather than
# milliseconds. A connector configured with engine="mysql" points at either
# server, and the difference does not surface until connect time — where an
# unknown system variable raises ERROR 1193 and takes the connector down
# entirely rather than degrading it.
#
# Verified against a real MariaDB 10.5 rather than assumed. The
# security-relevant statement, `SET SESSION TRANSACTION READ ONLY`, IS
# identical on both: a write inside such a session fails with ERROR 1792.
#
# These tests drive the listener with a fake cursor, so they need no MariaDB
# container and run wherever the suite runs.


class _FakeCursor:
    """Records what was executed, and rejects variables a server lacks."""

    def __init__(self, unknown: tuple[str, ...]) -> None:
        self.executed: list[str] = []
        self._unknown = unknown

    def execute(self, statement: str) -> None:
        for name in self._unknown:
            if name in statement:
                raise RuntimeError(f"1193 Unknown system variable '{name}'")
        self.executed.append(statement)

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _FakeConnection:
    def __init__(self, unknown: tuple[str, ...] = ()) -> None:
        self.cursor_obj = _FakeCursor(unknown)

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj


def _run_listener(unknown: tuple[str, ...]) -> list[str]:
    """Install the connect listener on a throwaway engine and fire it."""
    from sqlalchemy import create_engine as _create_engine

    from app.connectors.sql.connector import _install_mysql_session_settings

    config = SQLConnectorConfig(
        id="probe",
        display_name="probe",
        host="127.0.0.1",
        port=MYSQL_PORT,
        database="curated",
        username="analytics_readonly",
        password=SecretStr("unused"),
        schema="curated",
        required_labels=("analytics",),
        egress=EgressPolicy(allow_private=True, allow_loopback=True),
        engine="mysql",
        statement_timeout_ms=5000,
    )
    # A URL that is never connected to; the listener is invoked directly.
    # `connect` is a Pool event, so registered functions hang off
    # engine.pool.dispatch rather than engine.dispatch.
    #
    # Only OUR listener is invoked. The sqlite dialect registers its own
    # `connect` handler on this engine, and calling that one against a fake
    # connection fails on an unrelated sqlite API — a failure about
    # create_function, not about anything this test is checking.
    engine = _create_engine("sqlite://")
    before = set(engine.pool.dispatch.connect)
    _install_mysql_session_settings(engine, config)
    added = [fn for fn in engine.pool.dispatch.connect if fn not in before]

    assert len(added) == 1, f"expected exactly one new connect listener, got {len(added)}"

    conn = _FakeConnection(unknown)
    added[0](conn, None)
    engine.dispose()
    return conn.cursor_obj.executed


def test_read_only_is_set_before_anything_else() -> None:
    """The security-relevant statement must not depend on the timeout working.

    If the timeout were set first and raised, the connection would be left
    writable — the one outcome this listener exists to prevent.
    """
    executed = _run_listener(unknown=())
    assert executed, "the listener executed nothing"
    assert executed[0] == "SET SESSION TRANSACTION READ ONLY"


def test_a_mysql_server_gets_max_execution_time() -> None:
    """Milliseconds, MySQL's name for it."""
    executed = _run_listener(unknown=())
    assert any("max_execution_time = 5000" in s for s in executed)
    assert not any("max_statement_time" in s for s in executed), (
        "both timeout forms were applied; only the server's own should be"
    )


def test_a_mariadb_server_falls_back_to_max_statement_time() -> None:
    """MariaDB rejects max_execution_time, so the seconds-based name applies.

    5000ms becomes 5.0 seconds. A unit error here would set a five-thousand
    second timeout — silently useless rather than loudly broken.
    """
    executed = _run_listener(unknown=("max_execution_time",))
    assert executed[0] == "SET SESSION TRANSACTION READ ONLY"
    assert any("max_statement_time = 5.0" in s for s in executed), (
        f"MariaDB fallback did not apply: {executed}"
    )


def test_a_server_with_neither_still_starts_read_only() -> None:
    """A timeout hint is not worth refusing every connection over.

    The query remains bounded by the pool timeout and by LIMIT injection, and
    read-only — the actual guarantee — is unaffected.
    """
    executed = _run_listener(unknown=("max_execution_time", "max_statement_time"))
    assert executed == ["SET SESSION TRANSACTION READ ONLY"]
