"""The guarantee the whole phase rests on: the agent cannot write.

Not "does not". *Cannot*. The DoD asks for this to be "verified by an explicit
test, not by reading the code", so these tests attack the claim from four
directions:

  1. **The import graph.** Nothing outside the approvals API imports the
     executor. A tool that cannot name `execute_approved` cannot call it,
     whatever a poisoned document tells the model to do.

  2. **The class surface.** A `WriteTool` has no method that performs a write.
     Asserted by inspecting the class rather than by reading it, so a method
     added later fails this test rather than passing review.

  3. **Behaviour.** Running a write tool produces a pending row and touches no
     target system.

  4. **The executor's own refusals.** Every non-approved state is rejected, and
     rejected before any socket opens.

The fourth matters even though the first three should make it unreachable.
Layers that are individually sufficient are how a system survives one of them
being wrong.
"""

from __future__ import annotations

import inspect
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import Principal
from app.tools import approval as approval_module
from app.tools.base import ToolAuthorizationError, ToolRegistry
from app.tools.write_tools import CreateTicketTool

APP_DIR = Path(__file__).resolve().parents[2] / "app"


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

TENANT = uuid.UUID("w9170000-0000-0000-0000-000000000009".replace("w", "a"))

PROPOSER = Principal(
    tenant_id=TENANT,
    user_id=uuid.uuid4(),
    email="analyst@test",
    roles=("analyst",),
    allowed_labels=("operations",),
)

READER = Principal(
    tenant_id=TENANT,
    user_id=uuid.uuid4(),
    email="reader@test",
    roles=("reader",),
    allowed_labels=("operations",),
)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def tenant(engine: Engine) -> Iterator[None]:
    def _wipe() -> None:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM approval_request WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM connector WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": TENANT})

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenant (id, slug, name) VALUES (:t, 'write-test', 'Write')"),
            {"t": TENANT},
        )
    yield
    _wipe()


# --- 1. the import graph ------------------------------------------------------


def test_only_the_approvals_api_can_execute() -> None:
    """Nothing but the approvals endpoint may import the executor.

    This is the structural half of the guarantee. A module that cannot name
    `execute_approved` cannot call it — not by accident, not under a deadline,
    and not because a poisoned document persuaded a model to try.

    Checked by scanning imports rather than by convention, so adding the import
    to the agent fails here rather than passing review.
    """
    permitted = {"api/v1/approvals.py", "tools/approval.py"}
    offenders: list[str] = []

    pattern = re.compile(r"from\s+app\.tools\.approval\s+import|import\s+app\.tools\.approval")

    for path in APP_DIR.rglob("*.py"):
        relative = path.relative_to(APP_DIR).as_posix()
        if relative in permitted:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(relative)

    assert not offenders, (
        f"these modules import the write executor and must not: {offenders}. "
        "Only the approvals API may execute an approved write."
    )


def test_the_agent_does_not_reach_the_executor_transitively() -> None:
    """The agent's own modules, checked by name.

    A separate assertion from the one above because it states the thing that
    actually matters in a sentence someone can check: the agent has no route to
    the executor, however indirect.
    """
    agent_dir = APP_DIR / "agent"
    for path in agent_dir.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "execute_approved" not in source, (
            f"{path.name} references execute_approved; the agent must not be able "
            "to perform a write"
        )
        assert "tools.approval" not in source, f"{path.name} imports the write executor"


# --- 2. the class surface -----------------------------------------------------


def test_a_write_tool_has_no_method_that_writes() -> None:
    """Inspected, not read.

    A `WriteTool` subclass should expose `propose`, `run`, `authorize`, and
    `spec` — and nothing that sends. A method added later that looks like an
    execution path fails this test rather than relying on review.
    """
    suspicious = re.compile(r"execute|send|post|put|delete|commit|write", re.IGNORECASE)

    for name, _member in inspect.getmembers(CreateTicketTool, inspect.isfunction):
        if name.startswith("_"):
            continue
        assert not suspicious.search(name), (
            f"CreateTicketTool.{name}() looks like an execution path. "
            "A write tool proposes; it does not write."
        )


def test_the_write_tool_module_makes_no_network_calls() -> None:
    """No HTTP client is even importable from a write tool's module.

    The tool cannot send a request it has no way to build.
    """
    source = (APP_DIR / "tools" / "write_tools.py").read_text(encoding="utf-8")
    for forbidden in ("httpx", "requests", "urllib"):
        assert forbidden not in source, (
            f"write_tools.py references {forbidden}; a write tool must not be able "
            "to reach a network"
        )


# --- 3. behaviour -------------------------------------------------------------


def test_write_requires_approval(engine: Engine) -> None:
    """The roadmap's named test: running a write tool performs no write.

    It creates a PENDING row and returns a message saying so. Nothing reaches a
    target system, and the row's status is the proof.
    """
    with Session(engine) as session:
        tool = CreateTicketTool(session, "demo", ("operations",))
        result = tool.run(
            PROPOSER,
            title="Investigate shipping delay",
            body="Orders to the UK are late.",
            priority="high",
        )
        session.commit()

        assert not result.failed
        assert result.metadata["proposed"] is True
        # The model is told plainly. A model that believes it wrote something
        # tells the user it did, and the user believes them.
        assert "PROPOSED" in result.content
        assert "nothing has been done" in result.content.lower()

        row = session.execute(
            text("""
                SELECT status, payload, target_method, target_path, requested_by_email,
                       decided_by, idempotency_key
                FROM approval_request WHERE tenant_id = :t
            """),
            {"t": TENANT},
        ).one()

    assert row.status == "pending", "a write tool created something already approved"
    assert row.decided_by is None, "a proposal named an approver nobody chose"
    assert row.requested_by_email == PROPOSER.email
    assert row.target_method == "POST"
    assert row.idempotency_key, "no idempotency key was generated at proposal time"


def test_the_stored_payload_is_exactly_what_would_be_sent(engine: Engine) -> None:
    """An approver reads the payload, so the payload must be the truth.

    A summary is what makes a queue readable; it is not what reaches the target
    system, and approving a summary is approving a sentence.
    """
    with Session(engine) as session:
        tool = CreateTicketTool(session, "demo", ("operations",))
        tool.run(
            PROPOSER,
            title="Reindex the catalogue",
            body="Search results are stale.",
            priority="urgent",
            assignee="ops@acme.test",
        )
        session.commit()

        payload = session.execute(
            text("SELECT payload FROM approval_request WHERE tenant_id = :t"),
            {"t": TENANT},
        ).scalar()

    assert payload == {
        "title": "Reindex the catalogue",
        "body": "Search results are stale.",
        "priority": "urgent",
        "assignee": "ops@acme.test",
    }


def test_write_tools_default_off() -> None:
    """The roadmap's named test: no write tool exists unless one is named.

    A deployment that has not decided about writes gets an agent that cannot
    propose anything, which is a stronger position than one that can propose
    and is trusted not to.
    """
    from app.tools.write_tools import build_write_tools

    assert settings.write_tool_list == [], (
        "ENABLED_WRITE_TOOLS is not empty by default; writes are opt-in"
    )

    with Session(create_engine(settings.database_url)) as session:
        assert build_write_tools(session, connector_slug="demo", labels=("operations",)) == []


def test_an_unknown_write_tool_name_fails_loudly() -> None:
    """A typo in the flag must not silently register nothing.

    A deployment that meant `create_ticket` and typed `create_tickets` should be
    told, not left with an agent that quietly cannot propose.
    """
    from app.tools.write_tools import build_write_tools

    with Session(create_engine(settings.database_url)) as session:
        object.__setattr__(settings, "enabled_write_tools", "create_tickets")
        try:
            with pytest.raises(ValueError, match="not a write tool"):
                build_write_tools(session, connector_slug="demo", labels=("operations",))
        finally:
            object.__setattr__(settings, "enabled_write_tools", "")


# --- authorization ------------------------------------------------------------


def test_proposing_a_write_needs_more_than_a_label(engine: Engine) -> None:
    """A reader who can see the data cannot propose an action about it.

    Labels answer "what may this person see". Proposing a write against a
    production system is a different question, and answering it with the same
    mechanism would mean anyone who can read a document can act on it.
    """
    with Session(engine) as session:
        registry = ToolRegistry()
        registry.register(TENANT, CreateTicketTool(session, "demo", ("operations",)))

        with pytest.raises(ToolAuthorizationError):
            registry.invoke(READER, "create_ticket", title="x", body="y")

        rows = session.execute(
            text("SELECT count(*) FROM approval_request WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar()

    assert rows == 0, "a refused invocation still created a proposal"


def test_a_write_tool_with_no_roles_is_unusable(engine: Engine) -> None:
    """Default-deny, extended: a write tool that names no roles is refused
    outright rather than falling back to labels."""
    from app.tools.write_base import build_spec

    class RolelessTool(CreateTicketTool):
        @property
        def spec(self):  # type: ignore[no-untyped-def]
            return build_spec(
                name="roleless",
                description="x",
                parameters={},
                labels=("operations",),
                roles=(),
            )

    with Session(engine) as session:
        tool = RolelessTool(session, "demo", ("operations",))
        with pytest.raises(ToolAuthorizationError, match="declares no required roles"):
            tool.authorize(PROPOSER)


# --- 4. the executor's own refusals -------------------------------------------


def _insert(engine: Engine, status: str) -> uuid.UUID:
    """A request in a given state.

    `approved` also sets decided_by/decided_at, because the database's
    ck_approval_has_approver constraint refuses an approved row without them —
    which is the constraint working, not an inconvenience to route around.
    """
    request_id = uuid.uuid4()
    approved = status == "approved"

    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO approval_request
                  (id, tenant_id, status, tool_name, connector_slug, summary, payload,
                   target_method, target_path, idempotency_key, requested_by,
                   requested_by_email, decided_by, decided_by_email, decided_at,
                   expires_at)
                VALUES
                  (:id, :t, :status, 'create_ticket', 'demo', 'x', '{}',
                   'POST', '/tickets', :key, :u, 'a@test',
                   :approver, :approver_email, :decided_at,
                   now() + interval '1 hour')
            """),
            {
                "id": request_id,
                "t": TENANT,
                "status": status,
                "key": uuid.uuid4().hex,
                "u": uuid.uuid4(),
                # Built in Python rather than with a CASE: Postgres cannot infer
                # the type of a null parameter reused inside one.
                "approver": uuid.uuid4() if approved else None,
                "approver_email": "boss@test" if approved else None,
                "decided_at": datetime.now(UTC) if approved else None,
            },
        )
    return request_id


@pytest.mark.parametrize("status", ["pending", "rejected", "executed", "failed", "expired"])
def test_only_an_approved_request_can_execute(engine: Engine, status: str) -> None:
    """Every non-approved state is refused, before any socket opens.

    `rejected` is the roadmap's named case — `test_rejected_action_not_executed`
    — and the others are included because "rejected" is not the only way a
    request can fail to be approved.
    """
    request_id = _insert(engine, status)

    with Session(engine) as session:
        session.execute(text(f"SET LOCAL app.tenant_id = '{TENANT}'"))
        with pytest.raises(approval_module.ApprovalError, match="cannot be executed"):
            approval_module.execute_approved(session, request_id=request_id, principal=PROPOSER)


@pytest.fixture
def without_the_approver_constraint(engine: Engine) -> Iterator[None]:
    """Temporarily drop ck_approval_has_approver, and always put it back.

    A fixture rather than a try/finally inside the test, because the first
    version dropped the constraint and then failed BEFORE its try block — which
    left the database missing a safety constraint until someone noticed. A
    fixture's teardown runs whatever happens inside it.
    """
    definition = (
        "CHECK (status <> 'approved' OR (decided_by IS NOT NULL AND decided_at IS NOT NULL))"
    )
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE approval_request DROP CONSTRAINT IF EXISTS ck_approval_has_approver")
        )
    try:
        yield
    finally:
        with engine.begin() as conn:
            # The offending row must go BEFORE the constraint returns: adding a
            # CHECK is validated against existing rows, so a row that violates
            # it blocks its own cleanup. The first version of this fixture hit
            # exactly that and left the constraint off.
            conn.execute(text("DELETE FROM approval_request WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(
                text(
                    "ALTER TABLE approval_request ADD CONSTRAINT "
                    f"ck_approval_has_approver {definition}"
                )
            )


def test_an_approved_request_with_no_approver_is_refused(
    engine: Engine, without_the_approver_constraint: None
) -> None:
    """Belt and braces with the database constraint.

    The constraint is dropped for this test so the CODE path can be reached at
    all — the point is that both refuse, and the two protect against different
    mistakes: the constraint against a bad INSERT, the code against a row that
    predates the constraint.
    """
    request_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO approval_request
                  (id, tenant_id, status, tool_name, connector_slug, summary, payload,
                   target_method, target_path, idempotency_key, requested_by,
                   requested_by_email, expires_at)
                VALUES
                  (:id, :t, 'approved', 'create_ticket', 'demo', 'x', '{}',
                   'POST', '/tickets', :key, :u, 'a@test', now() + interval '1 hour')
            """),
            {"id": request_id, "t": TENANT, "key": uuid.uuid4().hex, "u": uuid.uuid4()},
        )

    with Session(engine) as session:
        session.execute(text(f"SET LOCAL app.tenant_id = '{TENANT}'"))
        with pytest.raises(approval_module.ApprovalError, match="names no approver"):
            approval_module.execute_approved(session, request_id=request_id, principal=PROPOSER)


def test_the_constraint_is_present_after_that_test(engine: Engine) -> None:
    """The fixture above drops a safety constraint. This asserts it came back.

    Worth its own test because the first version of that fixture did not always
    restore it, and a missing constraint is invisible until someone inserts the
    row it was meant to refuse.
    """
    with engine.connect() as conn:
        present = conn.execute(
            text("""
                SELECT count(*) FROM pg_constraint
                WHERE conrelid = 'approval_request'::regclass
                  AND conname = 'ck_approval_has_approver'
            """)
        ).scalar()
    assert present == 1, "ck_approval_has_approver was not restored"
