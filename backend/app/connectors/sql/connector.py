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

from pydantic import SecretStr
from sqlalchemy import create_engine, text
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
            connect_args={
                "connect_timeout": 5,
                # Belt and braces with the role-level setting: a role can be
                # altered, and this cannot be forgotten per-connection.
                "options": (
                    f"-c statement_timeout={config.statement_timeout_ms} "
                    "-c default_transaction_read_only=on"
                ),
            },
        )

    def _url(self) -> str:
        """Connect to the VALIDATED IP, not the hostname.

        Re-resolving here would reopen the DNS rebinding window that
        resolve_and_validate closed.
        """
        password = self._config.password.get_secret_value()
        return (
            f"postgresql+psycopg://{self._config.username}:{password}"
            f"@{self._target.ip}:{self._target.port}/{self._config.database}"
        )

    @property
    def info(self) -> ConnectorInfo:
        return ConnectorInfo(
            id=self._config.id,
            kind="postgres",
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
            safe = validate(sql, max_rows=self._config.max_rows)
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
        """
        self.authorize(principal)

        with self._engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT table_name, column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = :schema
                    ORDER BY table_name, ordinal_position
                """),
                {"schema": self._config.schema},
            ).all()

        tables: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            tables.setdefault(row.table_name, []).append(
                {"name": row.column_name, "type": row.data_type}
            )
        return [
            {"name": f"{self._config.schema}.{table}", "columns": columns}
            for table, columns in sorted(tables.items())
        ]

    def dispose(self) -> None:
        self._engine.dispose()
