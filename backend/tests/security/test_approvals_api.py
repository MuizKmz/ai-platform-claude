"""The approval queue, end to end, against a real target.

This is where the roadmap's remaining named tests live:

    test_approval_records_actor_and_payload
    test_idempotency_key_prevents_duplicate
    test_rejected_action_not_executed
    test_no_generated_sql_can_write

The write target is the demo API on port 9100 — a real HTTP service, not a
mock. Idempotency is a property of an HTTP conversation, and a mock that returns
whatever the test wants proves the platform CALLS something without proving that
calling twice creates one ticket. Here the test observes the behaviour rather
than asserting it.

Skipped when the demo API is not running, since a skipped test that would have
passed is better than a green one that never ran. Start it with:

    uv run --with fastapi --with uvicorn python -m uvicorn main:app \\
        --port 9100 --app-dir ../infra/demo-api
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import issue_token
from app.main import app
from app.tools.write_tools import CreateTicketTool

DEMO_API = "http://127.0.0.1:9100"


def _demo_api_available() -> bool:
    try:
        return httpx.get(f"{DEMO_API}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


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
    pytest.mark.skipif(not _demo_api_available(), reason="demo API not running on :9100"),
]

TENANT = uuid.UUID("a9970000-0000-0000-0000-000000000099")
OTHER = uuid.UUID("a9970000-0000-0000-0000-0000000000ff")

ANALYST_ID = uuid.UUID("a9970000-0000-0000-0000-0000000000a1")
ADMIN_ID = uuid.UUID("a9970000-0000-0000-0000-0000000000b2")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean(engine: Engine) -> Iterator[None]:
    httpx.post(f"{DEMO_API}/_reset", timeout=5)

    def _wipe() -> None:
        with engine.begin() as conn:
            params = {"a": TENANT, "b": OTHER}
            conn.execute(text("DELETE FROM approval_request WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM connector WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), params)

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO tenant (id, slug, name) VALUES
                  (:a, 'appr-test', 'Approvals'), (:b, 'appr-other', 'Other')
            """),
            {"a": TENANT, "b": OTHER},
        )
        # The write target, as a connector. Loopback is opted into explicitly,
        # exactly as a local development connector must be.
        conn.execute(
            text("""
                INSERT INTO connector
                  (id, tenant_id, kind, slug, display_name, required_labels, settings)
                VALUES (:id, :t, 'rest', 'demo', 'Demo ticketing', ARRAY['operations'],
                        CAST(:settings AS jsonb))
            """),
            {
                "id": uuid.uuid4(),
                "t": TENANT,
                "settings": (
                    '{"base_url": "http://127.0.0.1:9100", "endpoints": [], '
                    '"allow_private": true, "allow_loopback": true}'
                ),
            },
        )
    yield
    _wipe()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _headers(
    *, admin: bool = False, user_id: uuid.UUID | None = None, tenant: uuid.UUID = TENANT
) -> dict[str, str]:
    token = issue_token(
        tenant_id=tenant,
        user_id=user_id or (ADMIN_ID if admin else ANALYST_ID),
        email="boss@test" if admin else "analyst@test",
        roles=("admin",) if admin else ("analyst",),
        allowed_labels=("operations",),
    )
    return {"Authorization": f"Bearer {token}"}


def _propose(engine: Engine, title: str = "Investigate shipping delay") -> uuid.UUID:
    """Create a pending proposal the way the agent would: through the tool."""
    from app.core.security import Principal

    proposer = Principal(
        tenant_id=TENANT,
        user_id=ANALYST_ID,
        email="analyst@test",
        roles=("analyst",),
        allowed_labels=("operations",),
    )

    with Session(engine) as session:
        tool = CreateTicketTool(session, "demo", ("operations",))
        result = tool.run(proposer, title=title, body="Detail.", priority="high")
        session.commit()

    return uuid.UUID(result.metadata["approval_request_id"])


# --- the roadmap's named tests ------------------------------------------------


def test_approval_records_actor_and_payload(client: TestClient, engine: Engine) -> None:
    """The DoD: reconstruct who approved what, when, and why.

    All four, from one row, after the fact.
    """
    request_id = _propose(engine)

    response = client.post(
        f"/v1/approvals/{request_id}/approve",
        json={"note": "Confirmed with the warehouse team."},
        headers=_headers(admin=True),
    )
    assert response.status_code == 200, response.text

    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT status, decided_by, decided_by_email, decided_at, decision_note,
                       payload, requested_by_email, executed_at
                FROM approval_request WHERE id = :id
            """),
            {"id": request_id},
        ).one()

    # WHO
    assert row.decided_by == ADMIN_ID
    assert row.decided_by_email == "boss@test"
    # WHAT — the exact payload, still there after execution
    assert row.payload["title"] == "Investigate shipping delay"
    assert row.payload["priority"] == "high"
    # WHEN
    assert row.decided_at is not None
    assert row.executed_at is not None
    # WHY
    assert row.decision_note == "Confirmed with the warehouse team."
    # And who asked for it in the first place.
    assert row.requested_by_email == "analyst@test"
    assert row.status == "executed"


def test_idempotency_key_prevents_duplicate(client: TestClient, engine: Engine) -> None:
    """One approved proposal, at most one write — even sent twice.

    The second approve is refused by the status check before any request is
    made, so this asserts the belt AND the braces: the platform will not send
    twice, and if it did the key would make the target deduplicate.
    """
    request_id = _propose(engine, title="Restock alert")

    first = client.post(
        f"/v1/approvals/{request_id}/approve", json={}, headers=_headers(admin=True)
    )
    assert first.status_code == 200
    idempotency_key = first.json()["idempotency_key"]

    # A double-click. Refused: the request is no longer pending.
    second = client.post(
        f"/v1/approvals/{request_id}/approve", json={}, headers=_headers(admin=True)
    )
    assert second.status_code == 409

    # One ticket exists.
    tickets = httpx.get(f"{DEMO_API}/tickets", timeout=5).json()
    assert len(tickets) == 1, f"{len(tickets)} tickets were created from one approval"

    # The braces: replaying the same key against the target directly returns
    # the SAME ticket rather than creating another. This is what protects a
    # retried HTTP request or a worker that died mid-send.
    replay = httpx.post(
        f"{DEMO_API}/tickets",
        json={"title": "Restock alert", "body": "Detail.", "priority": "high"},
        headers={"Idempotency-Key": idempotency_key},
        timeout=5,
    )
    assert replay.status_code == 200, "a replayed key created a second ticket"
    assert replay.json()["id"] == tickets[0]["id"]
    assert len(httpx.get(f"{DEMO_API}/tickets", timeout=5).json()) == 1


def test_rejected_action_not_executed(client: TestClient, engine: Engine) -> None:
    """A rejected proposal never reaches the target, and cannot be revived."""
    request_id = _propose(engine, title="Delete the archive")

    rejected = client.post(
        f"/v1/approvals/{request_id}/reject",
        json={"note": "Too broad. Narrow the scope and re-propose."},
        headers=_headers(admin=True),
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    assert httpx.get(f"{DEMO_API}/tickets", timeout=5).json() == []

    # Terminal. Approving it afterwards is refused — an audit trail that records
    # only the last decision is not one.
    revived = client.post(
        f"/v1/approvals/{request_id}/approve", json={}, headers=_headers(admin=True)
    )
    assert revived.status_code == 409
    assert httpx.get(f"{DEMO_API}/tickets", timeout=5).json() == []


def test_no_generated_sql_can_write() -> None:
    """Phase 4's guarantee, re-verified now that writes exist.

    The read-only role is the control that stops generated SQL doing damage, and
    Phase 9 changes nothing about it. This bypasses the AST validator entirely
    and hands a write straight to the database — if it ever passes, every SQL
    safety test in this repository is decoration.
    """
    from sqlalchemy.exc import DatabaseError

    url = (
        f"postgresql+psycopg://analytics_readonly:{settings.postgres_readonly_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/analytics"
    )
    engine = create_engine(url)
    try:
        for statement in (
            "INSERT INTO curated.v_orders (order_id) VALUES (1)",
            "UPDATE curated.v_orders SET order_status = 'shipped'",
            "DELETE FROM curated.v_orders",
        ):
            with pytest.raises(DatabaseError), engine.begin() as conn:
                conn.execute(text(statement))
    finally:
        engine.dispose()


# --- authorization ------------------------------------------------------------


def test_approving_requires_admin(client: TestClient, engine: Engine) -> None:
    """The analyst who proposed it cannot approve it — nor can any non-admin."""
    request_id = _propose(engine)

    response = client.post(
        f"/v1/approvals/{request_id}/approve", json={}, headers=_headers(admin=False)
    )
    assert response.status_code == 403
    assert httpx.get(f"{DEMO_API}/tickets", timeout=5).json() == []


def test_an_admin_cannot_approve_their_own_proposal(client: TestClient, engine: Engine) -> None:
    """The second pair of eyes.

    An admin approving their own request is one pair looking twice, which is not
    what the mechanism is for. Asserted with an admin who is also the proposer,
    so the role check cannot be what refuses.
    """
    from app.core.security import Principal

    self_proposer = Principal(
        tenant_id=TENANT,
        user_id=ADMIN_ID,
        email="boss@test",
        roles=("admin",),
        allowed_labels=("operations",),
    )

    with Session(engine) as session:
        tool = CreateTicketTool(session, "demo", ("operations",))
        result = tool.run(self_proposer, title="Self-approved?", body="x", priority="low")
        session.commit()
    request_id = result.metadata["approval_request_id"]

    response = client.post(
        f"/v1/approvals/{request_id}/approve", json={}, headers=_headers(admin=True)
    )
    assert response.status_code == 403
    assert "cannot approve it" in response.json()["detail"]
    assert httpx.get(f"{DEMO_API}/tickets", timeout=5).json() == []


def test_another_tenants_request_is_invisible(client: TestClient, engine: Engine) -> None:
    """RLS scopes the queue. 404, not 403 — a different status would confirm it
    exists."""
    request_id = _propose(engine)

    response = client.get(
        f"/v1/approvals/{request_id}/dry-run", headers=_headers(admin=True, tenant=OTHER)
    )
    assert response.status_code == 404


def test_the_queue_requires_authentication(client: TestClient) -> None:
    assert client.get("/v1/approvals").status_code == 401


# --- dry run ------------------------------------------------------------------


def test_dry_run_shows_the_exact_payload(client: TestClient, engine: Engine) -> None:
    """The DoD: "dry-run shows the exact payload before approval".

    And it must change nothing — the name is literal.
    """
    request_id = _propose(engine, title="Check the conveyor")

    response = client.get(f"/v1/approvals/{request_id}/dry-run", headers=_headers(admin=True))
    assert response.status_code == 200

    body = response.json()
    assert body["payload"] == {
        "title": "Check the conveyor",
        "body": "Detail.",
        "priority": "high",
    }
    assert body["method"] == "POST"
    assert body["path"] == "/tickets"
    # Where it would actually go. "Create a ticket" means something different
    # against a staging host.
    assert body["target_base_url"] == "http://127.0.0.1:9100"
    assert body["actionable"] is True

    # Nothing happened.
    assert httpx.get(f"{DEMO_API}/tickets", timeout=5).json() == []
    with engine.connect() as conn:
        status_after = conn.execute(
            text("SELECT status FROM approval_request WHERE id = :id"), {"id": request_id}
        ).scalar()
    assert status_after == "pending"


def test_dry_run_requires_admin(client: TestClient, engine: Engine) -> None:
    """The payload is the sensitive part, and its audience is whoever decides."""
    request_id = _propose(engine)
    response = client.get(f"/v1/approvals/{request_id}/dry-run", headers=_headers(admin=False))
    assert response.status_code == 403


# --- the queue ----------------------------------------------------------------


def test_the_queue_omits_payloads(client: TestClient, engine: Engine) -> None:
    """Listing is for triage. Dumping every request body onto one screen adds
    sensitive detail without adding a decision."""
    _propose(engine)

    body = client.get("/v1/approvals", headers=_headers(admin=True)).json()
    assert len(body) == 1
    assert "payload" not in body[0]
    # But enough to triage: what, who, and when.
    assert body[0]["summary"]
    assert body[0]["requested_by_email"] == "analyst@test"
    assert body[0]["status"] == "pending"


def test_a_proposer_can_see_what_happened_to_their_request(
    client: TestClient, engine: Engine
) -> None:
    """An approval queue only an admin can read is one nobody chases."""
    _propose(engine)
    response = client.get("/v1/approvals", headers=_headers(admin=False))
    assert response.status_code == 200
    assert len(response.json()) == 1


# --- compensation -------------------------------------------------------------


def test_an_executed_write_can_be_undone(client: TestClient, engine: Engine) -> None:
    """The compensating action, against a target that supports one.

    The ticket is cancelled rather than deleted, and the row is marked
    compensated rather than reverted: "done and then undone" and "never
    happened" are different facts.
    """
    request_id = _propose(engine, title="Raised in error")

    approved = client.post(
        f"/v1/approvals/{request_id}/approve", json={}, headers=_headers(admin=True)
    )
    assert approved.status_code == 200
    ticket_id = approved.json()["response_body"]["id"]

    undone = client.post(
        f"/v1/approvals/{request_id}/compensate", json={}, headers=_headers(admin=True)
    )
    assert undone.status_code == 200, undone.text

    ticket = httpx.get(f"{DEMO_API}/tickets/{ticket_id}", timeout=5).json()
    assert ticket["status"] == "cancelled"
    assert ticket["cancelled_at"] is not None

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, compensated_at, compensated_by FROM approval_request WHERE id = :id"
            ),
            {"id": request_id},
        ).one()

    # Still `executed`. It happened, and then it was undone.
    assert row.status == "executed"
    assert row.compensated_at is not None
    assert row.compensated_by == ADMIN_ID


def test_compensating_twice_is_refused(client: TestClient, engine: Engine) -> None:
    request_id = _propose(engine)
    client.post(f"/v1/approvals/{request_id}/approve", json={}, headers=_headers(admin=True))
    client.post(f"/v1/approvals/{request_id}/compensate", json={}, headers=_headers(admin=True))

    again = client.post(
        f"/v1/approvals/{request_id}/compensate", json={}, headers=_headers(admin=True)
    )
    assert again.status_code == 409


def test_a_pending_request_cannot_be_compensated(client: TestClient, engine: Engine) -> None:
    """Undoing something that never happened is a state error, not a no-op."""
    request_id = _propose(engine)
    response = client.post(
        f"/v1/approvals/{request_id}/compensate", json={}, headers=_headers(admin=True)
    )
    assert response.status_code == 409


def test_an_expired_proposal_cannot_be_approved(client: TestClient, engine: Engine) -> None:
    """A stale proposal is refused before an approval is recorded.

    Found by auditing rather than by a failing test. The executor caught the
    expiry — nothing ran — but by then the row was marked approved, "WRITE
    APPROVED" had been logged for something that was never approvable, and the
    caller got a 502 implying the TARGET had failed when nothing was contacted.

    A stale proposal is a client error, and an audit trail should not record an
    approval that could never have executed.
    """
    request_id = _propose(engine, title="Stale request")

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE approval_request SET expires_at = now() - interval '2 hours' WHERE id = :id"
            ),
            {"id": request_id},
        )

    response = client.post(
        f"/v1/approvals/{request_id}/approve", json={}, headers=_headers(admin=True)
    )

    # 409, not 502: nothing downstream was contacted.
    assert response.status_code == 409
    assert "expired" in response.json()["detail"].lower()

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, decided_by, decided_at FROM approval_request WHERE id = :id"),
            {"id": request_id},
        ).one()

    assert row.status == "expired"
    # The crucial part: no approval was recorded for something unapprovable.
    assert row.decided_by is None, "an expired request was recorded as approved"
    assert row.decided_at is None

    assert httpx.get(f"{DEMO_API}/tickets", timeout=5).json() == []


def test_a_proposal_inside_its_window_is_still_approvable(
    client: TestClient, engine: Engine
) -> None:
    """The complement, so the expiry check cannot be satisfied by refusing
    everything."""
    request_id = _propose(engine, title="Fresh request")
    response = client.post(
        f"/v1/approvals/{request_id}/approve", json={}, headers=_headers(admin=True)
    )
    assert response.status_code == 200
    assert len(httpx.get(f"{DEMO_API}/tickets", timeout=5).json()) == 1


def test_dry_run_warns_when_the_target_is_unreachable(client: TestClient, engine: Engine) -> None:
    """An approver learns a connector is misconfigured BEFORE authorising.

    Added after noticing the egress check happened only at execution — so a
    blocked address produced a 502 on a request the approver had already
    authorised, and "WRITE APPROVED" was logged for something that could never
    have run. Dry-run resolves the address; it opens no socket.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE connector SET settings = jsonb_set("
                "settings, '{base_url}', '\"http://169.254.169.254\"') "
                "WHERE tenant_id = :t"
            ),
            {"t": TENANT},
        )

    request_id = _propose(engine, title="Points at cloud metadata")
    preview = client.get(f"/v1/approvals/{request_id}/dry-run", headers=_headers(admin=True)).json()

    assert preview["target_reachable"] is False
    assert "egress" in (preview["target_problem"] or "").lower()
    # Nothing was contacted, and nothing was decided.
    assert httpx.get(f"{DEMO_API}/tickets", timeout=5).json() == []


def test_dry_run_reports_a_reachable_target_as_reachable(
    client: TestClient, engine: Engine
) -> None:
    """The complement, so the check is not satisfied by always warning."""
    request_id = _propose(engine)
    preview = client.get(f"/v1/approvals/{request_id}/dry-run", headers=_headers(admin=True)).json()

    assert preview["target_reachable"] is True
    assert preview["target_problem"] is None


def test_concurrent_approvals_execute_once(client: TestClient, engine: Engine) -> None:
    """Three admins clicking Approve at the same moment produce ONE action.

    Found by firing concurrent requests rather than by reading the code. Without
    a row lock this is a check-then-act race: all three READ `pending`, all
    three pass the status check, and all three send a request to the target.

    That first run returned three 200s and created one ticket — but only because
    the idempotency key made the TARGET deduplicate. A backstop against a
    cooperative target is not a substitute for not sending three times, and a
    target with weaker idempotency would have had three tickets.

    `FOR UPDATE` in approve() makes the losers block until the winner commits,
    at which point they read `approved` and get a 409.
    """
    import threading

    request_id = _propose(engine, title="Concurrent approval")
    statuses: list[int] = []
    lock = threading.Lock()

    def attempt() -> None:
        # A distinct admin each time, so the self-approval guard is not what
        # refuses.
        headers = _headers(admin=True, user_id=uuid.uuid4())
        response = TestClient(app).post(
            f"/v1/approvals/{request_id}/approve", json={}, headers=headers
        )
        with lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 409, 409], (
        f"expected one winner and two conflicts, got {sorted(statuses)}"
    )

    tickets = httpx.get(f"{DEMO_API}/tickets", timeout=5).json()
    assert len(tickets) == 1, f"{len(tickets)} tickets from one approval"

    with engine.connect() as conn:
        decided = conn.execute(
            text("SELECT decided_by_email FROM approval_request WHERE id = :id"),
            {"id": request_id},
        ).scalar()
    assert decided is not None, "no approver was recorded"
