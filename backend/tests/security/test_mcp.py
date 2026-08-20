"""The MCP server: a second front door, the same locks.

The roadmap's named tests all live here. The one that matters most is
`test_mcp_tools_respect_authorization`, because the whole phase is the claim
that MCP is a *surface* rather than a second security model — and a surface
with its own authorization is a second security model wearing a different name.

So that test asserts the denials are identical: the same principal, the same
tool, the same refusal, through both doors.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.core.config import settings
from app.core.security import Principal, issue_token
from app.main import app
from app.mcp.auth import mcp_audience


def _database_available() -> bool:
    try:
        engine = create_engine(settings.database_url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


pytestmark = [
    pytest.mark.security,
    pytest.mark.skipif(not _database_available(), reason="no database reachable"),
]

TENANT = uuid.UUID("11cc0000-0000-0000-0000-0000000000cc")
OTHER = uuid.UUID("11cc0000-0000-0000-0000-0000000000ff")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def tenants(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from app.api.v1 import agent as agent_module
    from app.knowledge.embedding import FakeEmbeddings

    monkeypatch.setattr(agent_module, "get_embedding_provider", FakeEmbeddings)

    def _wipe() -> None:
        with engine.begin() as conn:
            params = {"a": TENANT, "b": OTHER}
            for table in ("chunk", "document", "approval_request", "connector"):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id IN (:a, :b)"),  # noqa: S608
                    params,
                )
            conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), params)

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO tenant (id, slug, name) VALUES
                  (:a, 'mcp-test', 'MCP'), (:b, 'mcp-other', 'Other')
            """),
            {"a": TENANT, "b": OTHER},
        )
        _seed_document(conn, TENANT, "Refunds take five business days.")
        _seed_document(conn, OTHER, "OTHER-TENANT-CANARY-4471")
    yield
    _wipe()


def _seed_document(conn: object, tenant: uuid.UUID, content: str) -> None:
    from app.knowledge.embedding import FakeEmbeddings

    provider = FakeEmbeddings()
    (vector,) = provider.embed([content])
    doc_id = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text("""
            INSERT INTO document (id, tenant_id, title, source_path, content_hash, labels)
            VALUES (:id, :t, 'doc', :src, :hash, ARRAY['public'])
        """),
        {"id": doc_id, "t": tenant, "src": f"/{tenant}", "hash": uuid.uuid4().hex},
    )
    conn.execute(  # type: ignore[attr-defined]
        text("""
            INSERT INTO chunk
              (id, tenant_id, document_id, ordinal, content, embedding,
               embedding_model, embedding_dim)
            VALUES (:id, :t, :d, 0, :content, :emb, :model, :dim)
        """),
        {
            "id": uuid.uuid4(),
            "t": tenant,
            "d": doc_id,
            "content": content,
            "emb": str(vector),
            "model": provider.model,
            "dim": provider.dimension,
        },
    )


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _principal(*, labels: tuple[str, ...] = ("public",), tenant: uuid.UUID = TENANT) -> Principal:
    return Principal(
        tenant_id=tenant,
        user_id=uuid.uuid4(),
        email="client@test",
        roles=("reader",),
        allowed_labels=labels,
    )


def _mcp_token(principal: Principal) -> str:
    return issue_token(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        email=principal.email,
        roles=principal.roles,
        allowed_labels=principal.allowed_labels,
        audience=mcp_audience(),
    )


def _api_token(principal: Principal) -> str:
    """A token for the platform API — deliberately the WRONG audience here."""
    return issue_token(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        email=principal.email,
        roles=principal.roles,
        allowed_labels=principal.allowed_labels,
    )


def _rpc(client: TestClient, method: str, token: str | None = None, **params: object) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers=headers,
    )
    return {"status": response.status_code, "body": response.json(), "headers": response.headers}


# --- the roadmap's named tests ------------------------------------------------


def test_mcp_token_audience_validated(client: TestClient) -> None:
    """A token for the platform API is not a token for the MCP server.

    RFC 8707. Accepting one here would mean a browser-session token doubles as a
    machine credential for a different surface, which is the confused-deputy
    problem the requirement exists to prevent.
    """
    principal = _principal()

    refused = _rpc(client, "tools/list", _api_token(principal))
    assert refused["status"] == 401
    assert refused["body"]["error"]["code"] == -32001

    # The same user, the same claims, the right audience: accepted.
    accepted = _rpc(client, "tools/list", _mcp_token(principal))
    assert accepted["status"] == 200
    assert "result" in accepted["body"]


def test_mcp_no_token_passthrough(client: TestClient, engine: Engine) -> None:
    """The token stops at the door.

    A gateway that forwards its caller's token lets that caller reach everything
    the gateway can reach, wearing the gateway's trust.

    Asserted against OUTBOUND requests only. The first version of this test
    spied on every httpx call and caught the TestClient's own request TO /mcp —
    which of course carries the token, because that is the request being
    authenticated. Filtering by host is what makes the assertion mean
    "forwarded onward" rather than "sent to us".
    """
    import unittest.mock

    import httpx

    principal = _principal()
    token = _mcp_token(principal)
    outbound: list[str] = []

    original = httpx.Client.request

    def spy(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
        target = str(url)
        # The TestClient's own request to the app under test is not outbound.
        if not target.startswith("http://testserver"):
            outbound.append(str(kwargs.get("headers") or {}))
        return original(self, method, url, **kwargs)

    with unittest.mock.patch.object(httpx.Client, "request", spy):
        result = _rpc(
            client,
            "tools/call",
            token,
            name="search_knowledge",
            arguments={"query": "refunds"},
        )

    assert result["status"] == 200

    for headers in outbound:
        assert token not in headers, "the caller's token was forwarded onward"
        # Nor a prefix of it, in case something truncated it into a header.
        assert token[:32] not in headers


def test_the_principal_carries_no_token(client: TestClient) -> None:
    """Nothing downstream can forward what it was never given.

    The structural half of the no-passthrough rule: a Principal has fields for
    identity and authority, and no field for the credential that proved them.
    """
    import dataclasses

    fields = {f.name for f in dataclasses.fields(Principal)}
    assert fields == {"tenant_id", "user_id", "email", "roles", "allowed_labels"}, (
        f"Principal gained a field: {fields}. If one of them holds a token, "
        "every tool that receives a Principal can forward it."
    )


def test_mcp_write_tools_not_exposed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A write tool is invisible over MCP, and refused if named anyway.

    An MCP client has no approval queue and no way to show a payload to a human.
    Exposing a tool that creates approval requests to a client that cannot
    complete the flow produces proposals nobody initiated a review for.
    """
    # Enabled for this test, so the assertion is about the MCP filter rather
    # than about the tool being absent everywhere.
    object.__setattr__(settings, "enabled_write_tools", "create_ticket")
    try:
        principal = _principal(labels=("public", "operations"))
        token = _mcp_token(principal)

        listed = _rpc(client, "tools/list", token)
        names = [t["name"] for t in listed["body"]["result"]["tools"]]
        assert "create_ticket" not in names, "a write tool was exposed over MCP"

        # Named anyway, by a client that learned it elsewhere.
        called = _rpc(
            client,
            "tools/call",
            token,
            name="create_ticket",
            arguments={"title": "x", "body": "y"},
        )
        result = called["body"]["result"]
        assert result["isError"] is True
        assert "do not have access" in result["content"][0]["text"]
    finally:
        object.__setattr__(settings, "enabled_write_tools", "")


def test_mcp_tools_respect_authorization(client: TestClient) -> None:
    """The same denials apply through MCP as internally.

    THE test of this phase. A surface with its own authorization is a second
    security model wearing a different name, so this asserts the refusal is
    identical — same principal, same tool, same message, both doors.
    """
    from app.api.v1.agent import build_registry
    from app.db.session import SessionLocal
    from app.tools.base import ToolAuthorizationError

    # A principal WITHOUT the analytics label.
    principal = _principal(labels=("public",))
    token = _mcp_token(principal)

    # Through MCP: not listed, and refused when named.
    listed = _rpc(client, "tools/list", token)
    names = [t["name"] for t in listed["body"]["result"]["tools"]]
    assert "query_database" not in names

    via_mcp = _rpc(client, "tools/call", token, name="query_database", arguments={"question": "x"})
    mcp_message = via_mcp["body"]["result"]["content"][0]["text"]
    assert via_mcp["body"]["result"]["isError"] is True

    # Through the internal path: the same registry, the same principal.
    with SessionLocal() as session:
        session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(principal.tenant_id)},
        )
        registry = build_registry(session, principal, _FakeLLM())
        try:
            registry.invoke(principal, "query_database", question="x")
            internal_message = "NOT REFUSED"
        except ToolAuthorizationError as exc:
            internal_message = str(exc)

    assert internal_message != "NOT REFUSED", "the internal path allowed it"
    # The same refusal, word for word. Not an equivalent check — the same one.
    assert mcp_message == internal_message


class _FakeLLM:
    @property
    def model(self) -> str:
        return "fake"

    def complete(self, messages: object, *, max_tokens: int = 1024) -> object:
        raise NotImplementedError

    def stream(self, messages: object, *, max_tokens: int = 1024) -> object:
        raise NotImplementedError

    def count_tokens(self, text_: str) -> int:
        return 1

    def cost_of(self, usage: object) -> float:
        return 0.0


def test_mcp_client_connector_no_abc_changes() -> None:
    """`connectors/base.py` is unchanged by this phase.

    Pinned by `test_connector_abc_unchanged` elsewhere; restated here because
    the roadmap names it, and because "MCP is just another connector" is only
    true if the ABC did not have to bend to accommodate it.
    """
    from pathlib import Path

    base = Path(__file__).resolve().parents[2] / "app" / "connectors" / "base.py"
    source = base.read_text(encoding="utf-8")
    assert "mcp" not in source.lower(), (
        "connectors/base.py mentions MCP; the ABC should not know about a specific protocol"
    )


# --- tenancy ------------------------------------------------------------------


def test_mcp_cannot_reach_another_tenant(client: TestClient) -> None:
    """A canary planted in another tenant is not retrievable through MCP.

    RLS scopes the session the handler builds, exactly as it scopes the
    request-dependency session the console uses.
    """
    principal = _principal()
    result = _rpc(
        client,
        "tools/call",
        _mcp_token(principal),
        name="search_knowledge",
        arguments={"query": "OTHER-TENANT-CANARY-4471"},
    )
    text_out = result["body"]["result"]["content"][0]["text"]
    assert "OTHER-TENANT-CANARY-4471" not in text_out


# --- protocol -----------------------------------------------------------------


def test_initialize_needs_no_token(client: TestClient) -> None:
    """A client must be able to negotiate before deciding which token to fetch.

    It returns no tenant data — a protocol version and a server name.
    """
    result = _rpc(client, "initialize")
    assert result["status"] == 200
    assert result["body"]["result"]["protocolVersion"] == "2026-07-28"
    assert result["body"]["result"]["serverInfo"]["name"] == "eaip"


def test_an_unauthenticated_call_points_at_the_metadata(client: TestClient) -> None:
    """RFC 9728: a client without a token must be able to discover the audience
    to ask for. Without this header it would guess, and guess wrong."""
    result = _rpc(client, "tools/list")
    assert result["status"] == 401
    challenge = result["headers"]["www-authenticate"]
    assert "resource_metadata" in challenge
    assert mcp_audience() in challenge


def test_the_metadata_document_is_public(client: TestClient) -> None:
    """Unauthenticated by necessity, and it discloses no tenant data."""
    response = client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200

    body = response.json()
    assert body["resource"] == mcp_audience()
    assert body["authorization_servers"] == [settings.jwt_issuer]


def test_a_malformed_request_is_a_protocol_error(client: TestClient) -> None:
    response = client.post("/mcp", content=b"not json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700


def test_an_unknown_method_is_refused(client: TestClient) -> None:
    result = _rpc(client, "resources/list", _mcp_token(_principal()))
    assert result["status"] == 404
    assert result["body"]["error"]["code"] == -32601


def test_ping_works_without_a_token(client: TestClient) -> None:
    """Liveness must not require credentials, or a monitor needs one."""
    result = _rpc(client, "ping")
    assert result["status"] == 200
    assert result["body"]["result"] == {}
