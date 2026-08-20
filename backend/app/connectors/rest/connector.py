"""REST connector: GET only.

The second implementation of `Connector`, and its real purpose is to test whether
that interface is an abstraction or a description of the SQL connector. If
`base.py` needed editing to accommodate this, the interface was shaped around its
first implementation — worth discovering now, while it is cheap.

**GET only**, and not merely by convention: the method is not a parameter
anywhere in this module. There is no `method=` argument to get wrong, no
configuration field to set to POST, and nothing to review for having done so.
A capability that does not exist cannot be misused.

Endpoints are **configuration, not code**. Adding an API to the platform is a
config entry, which is the metadata-driven model the architecture doc argues for,
made concrete for the first time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx
from pydantic import SecretStr

from app.connectors.base import (
    Capability,
    Connector,
    ConnectorError,
    ConnectorInfo,
    QueryResult,
)
from app.connectors.egress import EgressPolicy, resolve_and_validate
from app.connectors.resilience import CircuitBreaker, RetryPolicy, call_with_resilience
from app.connectors.rest.shaping import ShapingPolicy, shape_response
from app.core.security import Principal


@dataclass(frozen=True)
class EndpointSpec:
    """One upstream endpoint, exposed as one tool.

    Declared as data so adding an endpoint is a configuration change. The path
    template names its parameters, and only those may be substituted — a caller
    cannot introduce a path segment that was never declared.
    """

    name: str
    path: str
    description: str
    # Names appearing as {placeholders} in `path`.
    path_params: tuple[str, ...] = ()
    # Query parameters this endpoint accepts. An allowlist: anything else is
    # dropped rather than forwarded, so a caller cannot smuggle in `?admin=true`.
    query_params: tuple[str, ...] = ()


class AuthKind(StrEnum):
    API_KEY_HEADER = "api_key_header"
    BEARER = "bearer"
    NONE = "none"


@dataclass(frozen=True)
class RESTConnectorConfig:
    id: str
    display_name: str
    base_url: str
    endpoints: tuple[EndpointSpec, ...]
    auth_kind: str = AuthKind.NONE
    # Header name for API-key auth, e.g. "X-API-Key".
    auth_header: str = "Authorization"
    credential: SecretStr | None = None
    required_labels: tuple[str, ...] = ()
    egress: EgressPolicy = field(default_factory=EgressPolicy)
    timeout_seconds: float = 10.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    shaping: ShapingPolicy = field(default_factory=ShapingPolicy)


class RESTConnector(Connector):
    """Read-only HTTP access to one upstream API."""

    def __init__(self, config: RESTConnectorConfig) -> None:
        self._config = config
        self._endpoints = {e.name: e for e in config.endpoints}
        self._breaker = CircuitBreaker()

        # Validated once at construction, as with the SQL connector: a connector
        # pointed at cloud metadata should fail at configuration time.
        parsed = httpx.URL(config.base_url)
        host = parsed.host
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._target = resolve_and_validate(host, port, config.egress)

        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            headers=self._auth_headers(),
            follow_redirects=False,
        )

    def _auth_headers(self) -> dict[str, str]:
        """Credentials are set once on the client, never passed per call.

        A credential threaded through call sites is one that eventually reaches a
        log line or an exception's arguments.
        """
        if self._config.credential is None or self._config.auth_kind == AuthKind.NONE:
            return {}
        secret = self._config.credential.get_secret_value()
        if self._config.auth_kind == AuthKind.BEARER:
            return {"Authorization": f"Bearer {secret}"}
        return {self._config.auth_header: secret}

    @property
    def info(self) -> ConnectorInfo:
        return ConnectorInfo(
            id=self._config.id,
            kind="rest",
            display_name=self._config.display_name,
            # QUERY_READ only. There is no write capability to declare, because
            # there is no code path that could serve one.
            capabilities=frozenset({Capability.QUERY_READ, Capability.SCHEMA_DISCOVERY}),
            required_labels=self._config.required_labels,
        )

    def health(self) -> bool:
        return self._breaker.state.value != "open"

    def describe_schema(self, principal: Principal) -> list[dict[str, Any]]:
        """The endpoints available, for tool selection."""
        self.authorize(principal)
        return [
            {
                "name": endpoint.name,
                "description": endpoint.description,
                "path_params": list(endpoint.path_params),
                "query_params": list(endpoint.query_params),
            }
            for endpoint in self._config.endpoints
        ]

    def call(
        self,
        principal: Principal,
        endpoint_name: str,
        *,
        path_params: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
    ) -> QueryResult:
        """Perform one GET against a declared endpoint."""
        self.authorize(principal)

        endpoint = self._endpoints.get(endpoint_name)
        if endpoint is None:
            # Does not name the endpoints that DO exist: a caller who may not use
            # this connector should not learn its shape by guessing.
            raise ConnectorError(f"No endpoint {endpoint_name!r} is available.")

        path = self._render_path(endpoint, path_params or {})
        params = self._filter_query(endpoint, query_params or {})

        started = time.perf_counter()
        response = call_with_resilience(
            lambda: self._get(path, params),
            breaker=self._breaker,
            retry=self._config.retry,
            is_retryable=_is_retryable,
        )
        duration_ms = (time.perf_counter() - started) * 1000

        return shape_response(
            response,
            request_description=f"{endpoint.name} {path}",
            policy=self._config.shaping,
            duration_ms=duration_ms,
        )

    def _render_path(self, endpoint: EndpointSpec, values: dict[str, str]) -> str:
        """Substitute declared path parameters, and nothing else.

        Values are URL-encoded and rejected if they contain a path separator: a
        parameter is one segment, and `../` or `/admin` would make it several.
        """
        rendered = endpoint.path
        for name in endpoint.path_params:
            if name not in values:
                raise ConnectorError(f"Missing required path parameter {name!r}.")
            value = str(values[name])
            if "/" in value or ".." in value:
                raise ConnectorError(f"Path parameter {name!r} contains an invalid character.")
            rendered = rendered.replace(f"{{{name}}}", httpx.URL(path=value).path.lstrip("/"))
        return rendered

    def _filter_query(self, endpoint: EndpointSpec, values: dict[str, str]) -> dict[str, str]:
        """Keep only declared query parameters.

        An allowlist rather than a passthrough: forwarding whatever a caller sends
        lets them reach features of the upstream API nobody chose to expose.
        """
        return {k: str(v) for k, v in values.items() if k in endpoint.query_params}

    def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        """The only HTTP call in this module, and it is a GET.

        The method is hardcoded rather than parameterised: there is nothing to
        misconfigure, and nothing to review for having been misconfigured.
        """
        try:
            response = self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise ConnectorError("The upstream system did not respond in time.") from exc
        except httpx.HTTPError as exc:
            # Never echoed: httpx messages contain the full URL, which may carry
            # an API key in a query string on some upstreams.
            raise ConnectorError("The upstream system could not be reached.") from exc

        if response.status_code >= 400:
            raise UpstreamStatusError(response.status_code)
        return response

    def dispose(self) -> None:
        self._client.close()


class UpstreamStatusError(ConnectorError):
    """A non-2xx response. Carries the status so retry can classify it."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"The upstream system returned status {status_code}.")


def _is_retryable(error: Exception) -> bool:
    """Only failures that might succeed on a second attempt.

    A 404 or a 401 will fail identically every time, and retrying it spends an
    upstream's capacity to learn nothing. 429 is retryable because it explicitly
    means "later"; 5xx because the upstream may be mid-restart.
    """
    if isinstance(error, UpstreamStatusError):
        return error.status_code == 429 or error.status_code >= 500
    # Timeouts and connection errors reach here already wrapped as ConnectorError.
    return isinstance(error, ConnectorError)
