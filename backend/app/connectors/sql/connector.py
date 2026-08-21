"""The read-only SQL connector.

Five layers stand between a generated statement and the database, listed here in
order of how much they are relied upon:

1. **A Postgres role granted SELECT on curated views and nothing else.** This is
   the layer that actually protects you. It is not in this file, because it is
   not code — it is a GRANT, verified by a test that bypasses everything below.
2. **`default_transaction_read_only = on`**, set on the role itself, so even a
   connection that forgets to ask for it starts read-only.
3. **`statement_timeout` and a row cap**, so a wrong query is slow-and-killed
   rather than an outage.
4. **AST validation** (`safety.py`), which rejects clearly and early.
5. **Mandatory LIMIT injection**, so a question never becomes an export.

Layers 4 and 5 are convenience and clarity. Layers 1–3 are the guarantee. If that
ordering were reversed — if the parser were the thing being trusted — one
unanticipated construction would be the whole security model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import DBAPIError, OperationalError

from app.connectors.base import (
    Capability,
    Connector,
    ConnectorError,
    ConnectorInfo,
    QueryResult,
)
from app.connectors.egress import EgressPolicy, resolve_and_validate
from app.connectors.sql.safety import MAX_ROWS, UnsafeSQLError, validate
from app.core.security import Principal


@dataclass(frozen=True)
class SQLConnectorConfig:
    """Everything needed to reach one database, read-only."""

    id: str
    display_name: str
    host: str
    port: int
    database: str
    username: str
    password: SecretStr
    # Schema holding the curated views. The role should have rights to nothing
    # else, so this is a convenience for query generation, not a boundary.
    schema: str = "curated"
    required_labels: tuple[str, ...] = ()
    egress: EgressPolicy = EgressPolicy()
    statement_timeout_ms: int = 5000
    max_rows: int = MAX_ROWS
    # Which database this speaks. Postgres was first; MySQL arrived because the
    # target systems run it (ADR 0008). The value decides the driver, the SQL
    # dialect the validator parses with, and how read-only is enforced on the
    # connection — the three things that genuinely differ.
    engine: str = "postgres"


def _connect_args(config: SQLConnectorConfig) -> dict[str, object]:
    """Per-connection settings, which differ by engine in one important way.

    **Postgres** has `default_transaction_read_only`, a role-level setting that
    makes every connection start read-only even if the code forgets to ask.
    Setting it again here is belt and braces: a role can be altered, and this
    cannot be forgotten per-connection.

    **MySQL has no equivalent.** There is no server-side "this role is
    read-only" flag. The replacement is `SET SESSION TRANSACTION READ ONLY`,
    issued via PyMySQL's `init_command` on every connect — which is why the
    driver's connect-argument surface mattered when choosing one (ADR 0008).

    In BOTH cases the actual control is the grant: `SELECT` on curated views
    and nothing else. These settings catch a user provisioned with more rights
    than intended; they do not replace provisioning it correctly.
    """
    if config.engine == "mysql":
        # The session settings are applied by a `connect` event rather than
        # here — see `_install_mysql_session_settings`. PyMySQL's init_command
        # takes ONE statement, and these are two: MySQL rejects both
        # `SET SESSION TRANSACTION READ ONLY, SESSION x = 1` and the
        # semicolon-separated form through that argument. Found by trying both.
        return {"connect_timeout": 5}

    return {
        "connect_timeout": 5,
        "options": (
            f"-c statement_timeout={config.statement_timeout_ms} "
            "-c default_transaction_read_only=on"
        ),
    }


def _install_mysql_session_settings(engine: Engine, config: SQLConnectorConfig) -> None:
    """Make every MySQL connection read-only and time-bounded.

    Postgres carries this on the ROLE — `default_transaction_read_only` — so a
    connection starts read-only whether or not the code remembers to ask. MySQL
    has no such setting, which is the one place the two engines differ in a way
    that matters to security.

    This is the replacement: a `connect` event, so the settings are applied to
    every connection the pool creates, including ones opened later to replace a
    dropped one. Setting them once after `create_engine` would cover the first
    connection and silently miss the rest.

    It is still belt and braces. The actual control is the grant — SELECT on
    curated views and nothing else — and this catches a user provisioned with
    more rights than intended.
    """

    @event.listens_for(engine, "connect")
    def _apply(dbapi_connection: Any, _record: Any) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            # MySQL bounds statements with max_execution_time (milliseconds),
            # and it applies to SELECTs — which is all this connector issues.
            cursor.execute(f"SET SESSION max_execution_time = {int(config.statement_timeout_ms)}")


class SQLConnector(Connector):
    """Read-only queries against one Postgres database."""

    def __init__(self, config: SQLConnectorConfig) -> None:
        self._config = config
        # Validated at construction, so a connector pointed at cloud metadata
        # fails at configuration time rather than at first query.
        self._target = resolve_and_validate(config.host, config.port, config.egress)
        self._engine = create_engine(
            self._url(),
            pool_pre_ping=True,
            connect_args=_connect_args(config),
        )

        if config.engine == "mysql":
            _install_mysql_session_settings(self._engine, config)

    def _url(self) -> str:
        """Connect to the VALIDATED IP, not the hostname.

        Re-resolving here would reopen the DNS rebinding window that
        resolve_and_validate closed.
        """
        password = quote_plus(self._config.password.get_secret_value())
        user = quote_plus(self._config.username)
        driver = "mysql+pymysql" if self._config.engine == "mysql" else "postgresql+psycopg"
        return (
            f"{driver}://{user}:{password}"
            f"@{self._target.ip}:{self._target.port}/{self._config.database}"
        )

    @property
    def info(self) -> ConnectorInfo:
        return ConnectorInfo(
            id=self._config.id,
            kind=self._config.engine,
            display_name=self._config.display_name,
            # QUERY_WRITE is never declared. Phase 9 is the earliest that changes.
            capabilities=frozenset({Capability.QUERY_READ, Capability.SCHEMA_DISCOVERY}),
            required_labels=self._config.required_labels,
        )

    def health(self) -> bool:
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 — health is a boolean, not a diagnosis
            return False
        return True

    def query(self, principal: Principal, sql: str) -> QueryResult:
        """Validate and execute a read-only statement.

        The Principal is required and is checked here rather than by the caller:
        a connector that can be queried without saying who is asking is one
        forgotten check away from answering anyone.
        """
        self.authorize(principal)

        try:
            safe = validate(
                sql,
                max_rows=self._config.max_rows,
                schema=self._config.schema,
                dialect=self._config.engine,
            )
        except UnsafeSQLError as exc:
            # Safe to surface: it describes SQL the caller supplied.
            raise ConnectorError(str(exc)) from exc

        started = time.perf_counter()
        try:
            with self._engine.connect() as conn:
                result = conn.execute(text(safe.sql))
                columns = tuple(result.keys())
                rows = tuple(tuple(row) for row in result.fetchall())
        except OperationalError as exc:
            if "statement timeout" in str(exc).lower():
                raise ConnectorError(
                    f"The query exceeded the {self._config.statement_timeout_ms}ms time limit."
                ) from exc
            raise ConnectorError("The database could not be reached.") from exc
        except DBAPIError as exc:
            # Includes the permission denials that layer 1 produces. The message
            # is not echoed: upstream errors name schemas, roles, and columns.
            raise ConnectorError(
                "The query was refused by the database. This connector has read "
                "access to curated views only."
            ) from exc

        duration_ms = (time.perf_counter() - started) * 1000
        return QueryResult(
            columns=columns,
            rows=rows,
            sql=safe.sql,
            row_count=len(rows),
            # A full page of results may be a truncated answer, and a user asking
            # "how many" deserves to know the difference.
            truncated=len(rows) >= self._config.max_rows,
            duration_ms=duration_ms,
        )

    def describe_schema(self, principal: Principal) -> list[dict[str, Any]]:
        """The curated views and their columns, for query drafting.

        Only what the role can actually reach: information_schema is filtered by
        privilege, so this cannot describe a table the connector could not query.
        Both engines filter it, on slightly different rules — Postgres by
        ownership and grant, MySQL by holding any privilege on the object — and
        `test_schema_discovery_shows_only_curated_views` asserts the outcome
        against a real MySQL rather than trusting that equivalence.

        The columns are aliased because MySQL returns `information_schema`
        column names in UPPERCASE and Postgres in lowercase. Without the alias,
        `row.table_name` raised AttributeError against MySQL — so the agent got
        an exception instead of a schema, on the one call it makes before
        drafting any query.

        Rows are unpacked positionally rather than by attribute: SQLAlchemy
        reserves short attribute names on Row (`.t` is its own deprecated
        alias), and positional unpacking cannot collide with any of them.
        """
        self.authorize(principal)

        with self._engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT table_name AS obj_name,
                           column_name AS col_name,
                           data_type AS col_type
                    FROM information_schema.columns
                    WHERE table_schema = :schema
                    ORDER BY table_name, ordinal_position
                """),
                {"schema": self._config.schema},
            ).all()

        tables: dict[str, list[dict[str, str]]] = {}
        for obj_name, col_name, col_type in rows:
            tables.setdefault(obj_name, []).append({"name": col_name, "type": col_type})
        return [
            {"name": f"{self._config.schema}.{table}", "columns": columns}
            for table, columns in sorted(tables.items())
        ]

    def dispose(self) -> None:
        self._engine.dispose()
