"""Stored connector configuration: validation, and building the real thing.

Two jobs, deliberately in one place.

**Validation on write.** `settings` is a JSONB column, which means the database
will accept any shape. A Pydantic model per kind is what stops that: a SQL
connector must name a host, a port, and a database, and one that does not is
rejected at the API rather than at first query — the difference between a
configuration error and a 3am incident.

**Construction on read.** A stored row becomes a live `SQLConnector` or
`RESTConnector`. This is the only place that mapping exists, so a new connector
kind is a new model plus a new branch here, and nothing in the API or the agent
changes.

Note what is NOT here: any path from a stored row back to a plaintext
credential for display. `build_connector` decrypts in order to connect, and
that value goes to the connector and nowhere else.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from app.connectors.base import Connector
from app.connectors.egress import EgressPolicy
from app.connectors.rest.connector import (
    AuthKind,
    EndpointSpec,
    RESTConnector,
    RESTConnectorConfig,
)
from app.connectors.sql.connector import SQLConnector, SQLConnectorConfig

# Kinds the platform knows how to build. A row whose kind is not here cannot be
# constructed, which is the failure mode you want if a row outlives the code
# that understood it.
KINDS = ("sql", "rest")


class SQLSettings(BaseModel):
    """Everything needed to reach one database, minus the password."""

    kind: Literal["sql"] = "sql"
    # Which SQL engine. Defaults to postgres so every connector stored before
    # MySQL support keeps working without a migration — an absent field means
    # what it always meant.
    #
    # Constrained to the engines the safety validator can parse. An unknown
    # value must fail here, at construction, rather than reaching sqlglot and
    # being refused per-query with a confusing message.
    engine: Literal["postgres", "mysql"] = "postgres"
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, max_length=64)
    username: str = Field(min_length=1, max_length=64)
    # The schema holding curated views. Not a security boundary — the read-only
    # role's grants are — but query generation needs to know where to look.
    schema_name: str = Field(default="curated", max_length=64)
    statement_timeout_ms: int = Field(default=5000, ge=100, le=60_000)
    max_rows: int = Field(default=1000, ge=1, le=10_000)
    allow_private: bool = False
    allow_loopback: bool = False


class RESTEndpoint(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=500)
    path_params: list[str] = Field(default_factory=list)
    query_params: list[str] = Field(default_factory=list)


class RESTSettings(BaseModel):
    """One upstream HTTP API, minus the credential."""

    kind: Literal["rest"] = "rest"
    base_url: str = Field(min_length=1, max_length=500)
    endpoints: list[RESTEndpoint] = Field(default_factory=list)
    auth_kind: str = AuthKind.NONE
    auth_header: str = Field(default="Authorization", max_length=64)
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    allow_private: bool = False
    allow_loopback: bool = False

    @field_validator("base_url")
    @classmethod
    def _must_be_http(cls, value: str) -> str:
        # Checked here as well as by the egress policy, because a scheme like
        # file:// or gopher:// is a different class of mistake than a blocked
        # address and deserves to fail with a message that says so.
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return value

    @field_validator("auth_kind")
    @classmethod
    def _known_auth(cls, value: str) -> str:
        allowed = {k.value for k in AuthKind}
        if value not in allowed:
            raise ValueError(f"auth_kind must be one of {sorted(allowed)}")
        return value


ConnectorSettings = Annotated[SQLSettings | RESTSettings, Field(discriminator="kind")]


def validate_settings(kind: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Validate raw settings for a kind, returning what should be stored.

    Raises `ValueError` with a readable message. The caller turns that into a
    422 — a configuration mistake should be legible to whoever made it, unlike
    an authorization failure, which should not be legible to anyone.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {list(KINDS)}")

    model: type[BaseModel] = SQLSettings if kind == "sql" else RESTSettings
    parsed = model.model_validate({**settings, "kind": kind})
    stored = parsed.model_dump()
    # `kind` lives in its own column; keeping a copy in the JSONB invites the
    # two to disagree.
    stored.pop("kind", None)
    return stored


def build_connector(
    *,
    kind: str,
    slug: str,
    display_name: str,
    settings: dict[str, Any],
    required_labels: tuple[str, ...],
    credential: SecretStr | None,
) -> Connector:
    """Turn a stored row into a live connector.

    Raises `EgressBlockedError` if the target is not permitted — at construction
    time, before any query, which is where a connector pointed at cloud metadata
    should fail.
    """
    if kind == "sql":
        parsed = SQLSettings.model_validate({**settings, "kind": "sql"})
        if credential is None:
            raise ValueError("a SQL connector requires a password")
        return SQLConnector(
            SQLConnectorConfig(
                id=slug,
                display_name=display_name,
                host=parsed.host,
                port=parsed.port,
                database=parsed.database,
                username=parsed.username,
                password=credential,
                schema=parsed.schema_name,
                required_labels=required_labels,
                egress=EgressPolicy(
                    allow_private=parsed.allow_private,
                    allow_loopback=parsed.allow_loopback,
                ),
                statement_timeout_ms=parsed.statement_timeout_ms,
                max_rows=parsed.max_rows,
                engine=parsed.engine,
            )
        )

    if kind == "rest":
        parsed_rest = RESTSettings.model_validate({**settings, "kind": "rest"})
        return RESTConnector(
            RESTConnectorConfig(
                id=slug,
                display_name=display_name,
                base_url=parsed_rest.base_url,
                endpoints=tuple(
                    EndpointSpec(
                        name=e.name,
                        path=e.path,
                        description=e.description,
                        path_params=tuple(e.path_params),
                        query_params=tuple(e.query_params),
                    )
                    for e in parsed_rest.endpoints
                ),
                auth_kind=parsed_rest.auth_kind,
                auth_header=parsed_rest.auth_header,
                credential=credential,
                required_labels=required_labels,
                egress=EgressPolicy(
                    allow_private=parsed_rest.allow_private,
                    allow_loopback=parsed_rest.allow_loopback,
                ),
                timeout_seconds=parsed_rest.timeout_seconds,
            )
        )

    raise ValueError(f"unknown connector kind {kind!r}")
