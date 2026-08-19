"""The Connector contract — the platform's most important interface.

Every external system reaches the platform through this. Three properties are
deliberate, and each exists because its opposite causes a specific failure.

**Stateless.** A connector holds configuration, never per-request state. A
connector that remembers the last caller is one bad interleaving away from
answering request B with request A's authority.

**Every method takes a Principal.** Not a tenant id, not a user id — the verified
identity object. A connector cannot be called without saying who is calling, so
"I forgot to pass the user" is a type error rather than a silent authorization
bypass.

**Capabilities are declared as data.** Callers ask `connector.capabilities`, never
`isinstance(connector, SQLConnector)`. Type-sniffing spreads knowledge of concrete
connector classes through the codebase, and the second implementation then
requires changes everywhere the first one was special-cased.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.security import Principal


class ConnectorError(Exception):
    """A connector failed.

    Messages reaching a caller must not carry connection strings, credentials, or
    upstream error text — those routinely contain hostnames and query fragments.
    """


class ConnectorAuthorizationError(ConnectorError):
    """The Principal may not perform this operation."""


class Capability(StrEnum):
    """What a connector can do. Declared, never inferred from its class."""

    # Read structured data. Everything in Phase 4 is this and only this.
    QUERY_READ = "query.read"
    # Describe available tables/views/fields, so a query can be drafted.
    SCHEMA_DISCOVERY = "schema.discovery"
    # Reserved. Nothing declares this until Phase 9, and the tool layer refuses
    # it unconditionally until then.
    QUERY_WRITE = "query.write"


@dataclass(frozen=True)
class ConnectorInfo:
    """What a connector is, for a registry listing or a console."""

    id: str
    kind: str
    display_name: str
    capabilities: frozenset[Capability]
    # Labels a caller must hold to use this connector at all. Empty means nobody,
    # matching the default-deny used for documents.
    required_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryResult:
    """Rows plus the provenance needed to judge them.

    `sql` is not optional and is always surfaced to the user. Published
    text-to-SQL accuracy is around 82% against ~93% for human experts, so roughly
    one generated query in five is wrong. An interface that hides the query
    presents a guess as an answer.
    """

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    sql: str
    row_count: int
    truncated: bool
    duration_ms: float


class Connector(ABC):
    """Base class for every external system integration."""

    @property
    @abstractmethod
    def info(self) -> ConnectorInfo:
        """Identity and declared capabilities."""

    @abstractmethod
    def health(self) -> bool:
        """True if the upstream system is reachable and credentials work."""

    def supports(self, capability: Capability) -> bool:
        return capability in self.info.capabilities

    def authorize(self, principal: Principal) -> None:
        """Raise unless this Principal may use this connector.

        Default-deny: a connector with no required labels is usable by nobody,
        matching how an unlabelled document is visible to nobody. Declaring
        access has to be a deliberate act.
        """
        required = self.info.required_labels
        if not required:
            raise ConnectorAuthorizationError(
                f"Connector {self.info.id} declares no required labels and is "
                "therefore unusable. This is default-deny, not a misconfiguration "
                "to work around."
            )
        if not set(required) & set(principal.allowed_labels):
            raise ConnectorAuthorizationError(
                f"Principal lacks a label required by connector {self.info.id}."
            )


@dataclass
class ConnectorRegistry:
    """The connectors available to a tenant.

    Per-tenant rather than global: connector configuration names a tenant's
    systems and credentials, so one tenant must not be able to enumerate
    another's — even by id.
    """

    _by_tenant: dict[uuid.UUID, dict[str, Connector]] = field(default_factory=dict)

    def register(self, tenant_id: uuid.UUID, connector: Connector) -> None:
        self._by_tenant.setdefault(tenant_id, {})[connector.info.id] = connector

    def get(self, principal: Principal, connector_id: str) -> Connector:
        """Fetch a connector the Principal may use, or raise.

        A connector the caller cannot use raises the same error as one that does
        not exist — otherwise the registry confirms which systems a tenant has
        configured to someone not permitted to use them.
        """
        connector = self._by_tenant.get(principal.tenant_id, {}).get(connector_id)
        if connector is None:
            raise ConnectorAuthorizationError(f"No connector {connector_id!r} is available.")
        try:
            connector.authorize(principal)
        except ConnectorAuthorizationError as exc:
            raise ConnectorAuthorizationError(
                f"No connector {connector_id!r} is available."
            ) from exc
        return connector

    def list_for(self, principal: Principal) -> list[ConnectorInfo]:
        """Connectors this Principal may actually use — not merely those configured."""
        available = []
        for connector in self._by_tenant.get(principal.tenant_id, {}).values():
            try:
                connector.authorize(principal)
            except ConnectorAuthorizationError:
                continue
            available.append(connector.info)
        return available


# Process-wide registry. Populated at startup from configuration.
registry = ConnectorRegistry()
