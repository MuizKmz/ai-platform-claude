"""Table privileges, verified by attempting the operation.

Written after a GRANT statement in a migration proved not to mean what its
comment said. `init.sh` sets ALTER DEFAULT PRIVILEGES giving app_rw `arwd` and
app_readonly `SELECT` on every table created afterwards; a later migration that
GRANTs a narrower set does not remove what the default already gave.

So two migrations documented restrictions that were not in force:

  - `connector` was described as deliberately unreadable by app_readonly,
    "a credential column is not something to hand out by default"
  - `approval_request` was described as undeletable by app_rw so the
    application "cannot erase the trail"

Neither was exploited, and both are now true. The lesson is the shape of this
file: **a privilege is only verified by trying the thing**. Reading the GRANT
that a migration issues tells you what someone intended, not what the database
permits.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from app.core.config import settings

pytestmark = pytest.mark.security


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

ANY_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def app_rw() -> Iterator[Engine]:
    engine = create_engine(settings.app_database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def app_readonly() -> Iterator[Engine]:
    url = settings.app_database_url.replace("app_rw", "app_readonly").replace(
        settings.postgres_app_password, settings.postgres_readonly_password
    )
    engine = create_engine(url)
    yield engine
    engine.dispose()


def _permission_denied(engine: Engine, sql: str, *, tenant_context: bool = True) -> bool:
    """Attempt the statement. True if the DATABASE refused it.

    `WHERE false` on the destructive cases: the point is whether the statement
    is permitted, and a test that proves a privilege by destroying data is a
    test nobody runs twice.
    """
    try:
        with engine.begin() as conn:
            if tenant_context:
                conn.execute(text(f"SET LOCAL app.tenant_id = '{ANY_TENANT}'"))
            conn.execute(text(sql))
    except Exception as exc:
        return "permission denied" in str(exc).lower()
    return False


# --- the approval trail cannot be erased by the application -------------------


def test_the_application_cannot_delete_approval_records(app_rw: Engine) -> None:
    """An approval record is the evidence that a named human authorised a write
    which reached a production system.

    The application proposes, decides, and executes. It must not be able to
    remove the record of having done so — retention deletes these, and
    retention runs as the owner.
    """
    assert _permission_denied(app_rw, "DELETE FROM approval_request WHERE false"), (
        "app_rw can delete approval records; the audit trail is erasable"
    )


def test_the_application_can_still_write_approval_records(app_rw: Engine) -> None:
    """The complement. Revoking DELETE must not have taken INSERT or UPDATE
    with it — the flow depends on both."""
    assert not _permission_denied(app_rw, "SELECT count(*) FROM approval_request")
    assert not _permission_denied(
        app_rw, "UPDATE approval_request SET decision_note = decision_note WHERE false"
    )


# --- credentials are not handed to the reporting role -------------------------


def test_the_readonly_role_cannot_read_connector_configuration(
    app_readonly: Engine,
) -> None:
    """`connector` carries an encrypted credential column.

    app_readonly backs reporting paths; nothing there needs connector
    configuration, and a credential column is not something to hand out by
    default — even encrypted, even to a role that can only read.
    """
    assert _permission_denied(
        app_readonly, "SELECT count(*) FROM connector", tenant_context=False
    ), "app_readonly can read connector rows, including the credential column"


def test_the_readonly_role_cannot_read_the_approval_queue(
    app_readonly: Engine,
) -> None:
    """A proposal names a target system, a path, and a payload."""
    assert _permission_denied(
        app_readonly, "SELECT count(*) FROM approval_request", tenant_context=False
    )


def test_the_readonly_role_can_still_read_what_it_is_for(app_readonly: Engine) -> None:
    """The complement, so the two tests above are not satisfied by a role that
    can read nothing at all."""
    assert not _permission_denied(app_readonly, "SELECT count(*) FROM tenant", tenant_context=False)


# --- the application role never bypasses RLS ----------------------------------


def test_the_application_role_cannot_bypass_rls(app_rw: Engine) -> None:
    """Re-asserted here because every isolation guarantee rests on it, and a
    role attribute is exactly the kind of thing an operator changes once while
    debugging and forgets."""
    with app_rw.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert not row.rolsuper, "the application role is a superuser; RLS does not apply to it"
    assert not row.rolbypassrls, "the application role bypasses RLS"


# --- nothing waits forever ----------------------------------------------------


def test_both_engines_bound_statements_and_locks() -> None:
    """A query, and a wait for a row lock, both have a ceiling.

    Postgres defaults both to 0 — unlimited. The SQL connector has always set a
    statement_timeout for GENERATED queries; the application's own connection
    never did, which is the more dangerous omission because it affects every
    request rather than one tool.

    A `FOR UPDATE` blocked behind a stuck transaction would wait forever, and
    nothing in the stack would interrupt it. Approve and reject both take row
    locks, so this is on the write path.
    """
    # The APPLICATION's engines, not one this test builds. The first version
    # used the local app_rw fixture, which creates its own engine without the
    # connect options — so it tested a connection the application never uses
    # and failed for the wrong reason.
    from app.db.session import engine as app_engine
    from app.db.session import owner_engine

    for name, target in (("app_rw", app_engine), ("owner", owner_engine)):
        with target.connect() as conn:
            statement = conn.execute(text("SHOW statement_timeout")).scalar()
            lock = conn.execute(text("SHOW lock_timeout")).scalar()

        assert statement not in ("0", None), f"{name} has no statement timeout"
        assert lock not in ("0", None), f"{name} has no lock timeout"


def test_the_lock_timeout_is_shorter_than_the_statement_timeout() -> None:
    """Waiting seconds for a lock means someone else holds it.

    Failing fast with a readable error beats a request that hangs until a
    client gives up — so the lock ceiling is the tighter of the two.
    """
    from app.db.session import LOCK_TIMEOUT_MS, STATEMENT_TIMEOUT_MS

    assert LOCK_TIMEOUT_MS < STATEMENT_TIMEOUT_MS
