"""REST connector: GET-only enforcement, resilience, and the ABC test.

The most important test here is `test_connector_abc_unchanged`. Phase 5 exists to
find out whether `Connector` is an abstraction or a description of the SQL
connector, and the answer is a git hash rather than an opinion.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from app.connectors.base import ConnectorAuthorizationError, ConnectorError
from app.connectors.egress import EgressBlockedError, ResolvedTarget
from app.connectors.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    RetryPolicy,
    call_with_resilience,
)
from app.connectors.rest.connector import (
    AuthKind,
    EndpointSpec,
    RESTConnector,
    RESTConnectorConfig,
    UpstreamStatusError,
)
from app.connectors.rest.shaping import ShapingPolicy
from app.core.security import Principal

pytestmark = pytest.mark.security

ANALYST = Principal(
    tenant_id=__import__("uuid").uuid4(),
    user_id=__import__("uuid").uuid4(),
    email="analyst@test",
    roles=("reader",),
    allowed_labels=("orders",),
)

OUTSIDER = Principal(
    tenant_id=ANALYST.tenant_id,
    user_id=__import__("uuid").uuid4(),
    email="outsider@test",
    roles=("reader",),
    allowed_labels=("public",),
)

ENDPOINTS = (
    EndpointSpec(
        name="get_order",
        path="/orders/{order_id}",
        description="Fetch one order by id.",
        path_params=("order_id",),
    ),
    EndpointSpec(
        name="list_orders",
        path="/orders",
        description="List orders.",
        query_params=("status", "limit"),
    ),
)


def _config(**overrides: object) -> RESTConnectorConfig:
    base = {
        "id": "orders-api",
        "display_name": "Orders API",
        "base_url": "https://api.example.com",
        "endpoints": ENDPOINTS,
        "required_labels": ("orders",),
        "retry": RetryPolicy(attempts=1, jitter=False),
    }
    base.update(overrides)
    return RESTConnectorConfig(**base)  # type: ignore[arg-type]


def _build(config: RESTConnectorConfig, handler: object) -> RESTConnector:
    """Construct a connector with a stubbed transport.

    Egress is stubbed too: these tests exercise the REST connector, and egress
    has its own suite.
    """
    with patch(
        "app.connectors.rest.connector.resolve_and_validate",
        return_value=ResolvedTarget(host="api.example.com", ip="93.184.216.34", port=443),
    ):
        connector = RESTConnector(config)
    connector._client = httpx.Client(  # noqa: SLF001 — test seam
        base_url=config.base_url,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        headers=connector._client.headers,  # noqa: SLF001
    )
    return connector


@pytest.fixture
def json_connector() -> Iterator[RESTConnector]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 1, "status": "shipped"})

    connector = _build(_config(), handler)
    yield connector
    connector.dispose()


# --- the phase's central question ---------------------------------------------


def test_connector_abc_unchanged() -> None:
    """base.py must not have been modified to accommodate a second connector.

    An abstraction validated by one implementation is not an abstraction. If this
    fails, `Connector` was shaped around the SQL connector and the interface —
    not this test — is what needs changing.

    The hash is base.py as it stood when Phase 4 completed (commit 7c80357).
    """
    repo_root = Path(__file__).resolve().parents[3]
    expected = "64c4f9ee4a0590a4c476fb4d5b8996df9330571c"

    try:
        actual = subprocess.run(  # noqa: S603
            ["git", "hash-object", "backend/app/connectors/base.py"],  # noqa: S607
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        pytest.skip("git not available")

    assert actual == expected, (
        "connectors/base.py changed during or after Phase 5. If a second "
        "implementation required editing the interface, the interface was shaped "
        "around the first one — reconsider the design before updating this hash."
    )


# --- GET only -----------------------------------------------------------------


def test_only_get_permitted(json_connector: RESTConnector) -> None:
    """The connector exposes no way to issue anything but GET.

    Asserted structurally rather than by attempting a POST: there is no method
    parameter to pass, which is the stronger property. A capability that does not
    exist cannot be misconfigured.
    """
    public_api = {name for name in dir(json_connector) if not name.startswith("_")}

    assert "call" in public_api
    for forbidden in ("post", "put", "patch", "delete", "request", "send"):
        assert forbidden not in public_api


def test_the_connector_declares_no_write_capability(json_connector: RESTConnector) -> None:
    from app.connectors.base import Capability

    assert Capability.QUERY_WRITE not in json_connector.info.capabilities


def test_underlying_request_is_a_get() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200, json={"ok": True})

    connector = _build(_config(), handler)
    try:
        connector.call(ANALYST, "get_order", path_params={"order_id": "1"})
    finally:
        connector.dispose()

    assert seen == ["GET"]


# --- authorization ------------------------------------------------------------


def test_unauthorized_principal_is_refused(json_connector: RESTConnector) -> None:
    with pytest.raises(ConnectorAuthorizationError):
        json_connector.call(OUTSIDER, "get_order", path_params={"order_id": "1"})


def test_unknown_endpoint_does_not_reveal_the_known_ones(
    json_connector: RESTConnector,
) -> None:
    """A caller guessing endpoint names should learn nothing from the error."""
    with pytest.raises(ConnectorError) as exc:
        json_connector.call(ANALYST, "delete_everything")

    message = str(exc.value)
    assert "get_order" not in message
    assert "list_orders" not in message


# --- parameter handling -------------------------------------------------------


def test_path_traversal_in_a_parameter_is_refused(json_connector: RESTConnector) -> None:
    """A path parameter is one segment. `../` would make it several."""
    for value in ("../admin", "1/../../secrets", "a/b"):
        with pytest.raises(ConnectorError, match="invalid character"):
            json_connector.call(ANALYST, "get_order", path_params={"order_id": value})


def test_undeclared_query_parameters_are_dropped() -> None:
    """An allowlist, not a passthrough: a caller must not reach features of the
    upstream API that nobody chose to expose."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.params))
        return httpx.Response(200, json=[])

    connector = _build(_config(), handler)
    try:
        connector.call(
            ANALYST,
            "list_orders",
            query_params={"status": "shipped", "admin": "true", "internal": "1"},
        )
    finally:
        connector.dispose()

    assert "status=shipped" in seen[0]
    assert "admin" not in seen[0]
    assert "internal" not in seen[0]


def test_missing_path_parameter_is_refused(json_connector: RESTConnector) -> None:
    with pytest.raises(ConnectorError, match="Missing required path parameter"):
        json_connector.call(ANALYST, "get_order")


# --- credentials --------------------------------------------------------------


def test_api_key_never_logged() -> None:
    """The credential must not appear in repr, errors, or the result."""
    api_key = "sk-super-secret-api-key-value"  # noqa: S105 — a test fixture

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    connector = _build(
        _config(
            auth_kind=AuthKind.API_KEY_HEADER,
            auth_header="X-API-Key",
            credential=SecretStr(api_key),
        ),
        handler,
    )
    try:
        result = connector.call(ANALYST, "get_order", path_params={"order_id": "1"})
        assert api_key not in repr(connector)
        assert api_key not in str(result)
        assert api_key not in result.sql
    finally:
        connector.dispose()


def test_the_credential_does_reach_the_upstream() -> None:
    """The counterpart to the test above: it must be hidden, not lost."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-API-Key"))
        return httpx.Response(200, json={"ok": True})

    connector = _build(
        _config(
            auth_kind=AuthKind.API_KEY_HEADER,
            auth_header="X-API-Key",
            credential=SecretStr("the-key"),
        ),
        handler,
    )
    try:
        connector.call(ANALYST, "get_order", path_params={"order_id": "1"})
    finally:
        connector.dispose()

    assert seen == ["the-key"]


# --- egress -------------------------------------------------------------------


def test_egress_allowlist_applies() -> None:
    """The same control as the SQL connector, on a different protocol."""
    with (
        patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("169.254.169.254", 443))]),
        pytest.raises(EgressBlockedError),
    ):
        RESTConnector(_config(base_url="http://169.254.169.254"))


# --- response shaping ---------------------------------------------------------


def test_response_truncation_includes_guidance() -> None:
    """ "Truncated" alone makes a model repeat the same request.

    Naming the total and suggesting how to narrow lets it ask a better question.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": i, "status": "shipped"} for i in range(200)])

    connector = _build(_config(shaping=ShapingPolicy(max_items=10)), handler)
    try:
        result = connector.call(ANALYST, "list_orders")
    finally:
        connector.dispose()

    assert result.truncated is True
    notice = str(result.rows[-1])
    assert "10 of 200" in notice
    assert "narrow" in notice.lower()
    # Actionable, not merely descriptive.
    assert "same request returns the same" in notice


def test_small_responses_are_not_truncated(json_connector: RESTConnector) -> None:
    result = json_connector.call(ANALYST, "get_order", path_params={"order_id": "1"})

    assert result.truncated is False
    assert result.row_count == 1


def test_non_json_responses_are_handled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="plain text body")

    connector = _build(_config(), handler)
    try:
        result = connector.call(ANALYST, "get_order", path_params={"order_id": "1"})
    finally:
        connector.dispose()

    assert result.columns == ("body",)
    assert "plain text" in result.rows[0][0]


# --- resilience ---------------------------------------------------------------


def test_circuit_breaker_opens() -> None:
    """Repeated failures stop outbound calls entirely.

    A struggling upstream receives less load, not more, and the caller gets an
    immediate error rather than waiting for a timeout already known to be coming.
    """
    breaker = CircuitBreaker(failure_threshold=3)
    retry = RetryPolicy(attempts=1, jitter=False)

    def failing() -> None:
        raise UpstreamStatusError(503)

    for _ in range(3):
        with pytest.raises(UpstreamStatusError):
            call_with_resilience(failing, breaker=breaker, retry=retry, is_retryable=lambda _: True)

    assert breaker.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        call_with_resilience(failing, breaker=breaker, retry=retry, is_retryable=lambda _: True)


def test_breaker_probes_after_the_recovery_window() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=0.0)

    def failing() -> None:
        raise UpstreamStatusError(500)

    with pytest.raises(UpstreamStatusError):
        call_with_resilience(
            failing,
            breaker=breaker,
            retry=RetryPolicy(attempts=1, jitter=False),
            is_retryable=lambda _: True,
        )

    assert breaker.state is CircuitState.HALF_OPEN
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED


def test_permanent_errors_are_not_retried() -> None:
    """Retrying a 404 spends an upstream's capacity to learn nothing."""
    attempts = 0

    def failing() -> None:
        nonlocal attempts
        attempts += 1
        raise UpstreamStatusError(404)

    from app.connectors.rest.connector import _is_retryable

    with pytest.raises(UpstreamStatusError):
        call_with_resilience(
            failing,
            breaker=CircuitBreaker(),
            retry=RetryPolicy(attempts=3, jitter=False),
            is_retryable=_is_retryable,
        )

    assert attempts == 1


def test_transient_errors_are_retried() -> None:
    attempts = 0

    def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise UpstreamStatusError(503)
        return "ok"

    from app.connectors.rest.connector import _is_retryable

    result = call_with_resilience(
        flaky,
        breaker=CircuitBreaker(),
        retry=RetryPolicy(attempts=3, jitter=False),
        is_retryable=_is_retryable,
        sleep=lambda _: None,
    )

    assert result == "ok"
    assert attempts == 3


def test_backoff_grows_and_is_capped() -> None:
    policy = RetryPolicy(base_delay=1.0, max_delay=4.0, jitter=False)

    assert policy.delay_for(1) == 1.0
    assert policy.delay_for(2) == 2.0
    assert policy.delay_for(3) == 4.0
    assert policy.delay_for(10) == 4.0


def test_upstream_errors_are_not_echoed() -> None:
    """httpx messages contain the full URL, which can carry a key in a query."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed connecting to https://api.example.com?key=secret")

    connector = _build(_config(), handler)
    try:
        with pytest.raises(ConnectorError) as exc:
            connector.call(ANALYST, "get_order", path_params={"order_id": "1"})
    finally:
        connector.dispose()

    assert "secret" not in str(exc.value)


def test_health_reflects_the_breaker(json_connector: RESTConnector) -> None:
    """A connector outage must be visible rather than inferred from failures."""
    assert json_connector.health() is True

    for _ in range(5):
        json_connector._breaker.record_failure()  # noqa: SLF001 — test seam

    assert json_connector.health() is False
