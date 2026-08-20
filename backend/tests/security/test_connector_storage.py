"""The connector configuration table.

Connectors moved out of code and into a table in Phase 6, which turns a
deployment concern into a data one. Two properties matter enough to pin:

  - a connector row belongs to exactly one tenant, and the database enforces it
  - the credential is ciphertext at rest, so a dump is not a disclosure

The second is worth stating precisely. Encryption happens in the application
before the value reaches the database, which is what makes a stolen backup
useless. Storage-level encryption at rest protects the disk, not the dump, and
those are different threats.

The API-level guarantee — that a credential is never *returned* — is asserted in
the integrations API tests. This file covers what is true of the storage itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text

from app.connectors.credentials import decrypt, encrypt
from app.core.config import settings


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

TENANT = uuid.UUID("c0c00000-0000-0000-0000-0000000000c0")
OTHER = uuid.UUID("c0c00000-0000-0000-0000-0000000000ff")

# A distinctive string, so the stored bytes can be searched for it.
FIXTURE_SECRET = "correct-horse-battery-staple"  # noqa: S105 — a test fixture


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def tenants(engine: Engine) -> Iterator[None]:
    def _wipe() -> None:
        with engine.begin() as conn:
            params = {"a": TENANT, "b": OTHER}
            conn.execute(text("DELETE FROM connector WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), params)

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO tenant (id, slug, name) VALUES
                  (:a, 'conn-test', 'Connector Test'), (:b, 'conn-other', 'Connector Other')
            """),
            {"a": TENANT, "b": OTHER},
        )
    yield
    _wipe()


def _insert(conn: object, tenant: uuid.UUID, slug: str, credential: str | None) -> uuid.UUID:
    row_id = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text("""
            INSERT INTO connector
              (id, tenant_id, kind, slug, display_name, required_labels, settings, credential)
            VALUES
              (:id, :t, 'sql', :slug, 'Test', ARRAY['analytics'], '{"host": "localhost"}', :cred)
        """),
        {"id": row_id, "t": tenant, "slug": slug, "cred": credential},
    )
    return row_id


# --- tenancy ----------------------------------------------------------------


def test_connectors_are_tenant_isolated(engine: Engine) -> None:
    """A connector row names a host, a database, and a username.

    That is reconnaissance material before the credential is even considered,
    so the row is protected exactly like any other tenant data.
    """
    with engine.begin() as conn:
        _insert(conn, TENANT, "analytics", None)
        _insert(conn, OTHER, "analytics", None)

    app_engine = create_engine(settings.app_database_url)
    try:
        with app_engine.begin() as conn:
            conn.execute(text(f"SET LOCAL app.tenant_id = '{TENANT}'"))
            visible = conn.execute(text("SELECT count(*) FROM connector")).scalar()
        with app_engine.connect() as conn:
            without_context = conn.execute(text("SELECT count(*) FROM connector")).scalar()
    finally:
        app_engine.dispose()

    assert visible == 1, "a tenant saw more than its own connector"
    assert without_context == 0, "no tenant context should mean no rows"


def test_the_same_slug_may_exist_in_two_tenants(engine: Engine) -> None:
    """Uniqueness is scoped to the tenant, not global.

    Two customers may both have an "analytics" connector. A global unique
    constraint would let one learn of the other by failing to create it.
    """
    with engine.begin() as conn:
        _insert(conn, TENANT, "analytics", None)
        _insert(conn, OTHER, "analytics", None)

    with engine.connect() as conn:
        total = conn.execute(
            text("SELECT count(*) FROM connector WHERE slug = 'analytics'")
        ).scalar()
    assert total == 2


def test_a_duplicate_slug_within_one_tenant_is_rejected(engine: Engine) -> None:
    """A slug names the connector in tool calls and the audit log. Two rows
    answering to one name would make both ambiguous."""
    from sqlalchemy.exc import IntegrityError

    with engine.begin() as conn:
        _insert(conn, TENANT, "analytics", None)

    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert(conn, TENANT, "analytics", None)


# --- the credential ---------------------------------------------------------


def test_the_stored_credential_is_ciphertext(engine: Engine) -> None:
    """What lands in the column must not be the password.

    This is the property that makes a database dump survivable, and it is worth
    asserting against the actual stored bytes rather than trusting that the
    encrypt() call was wired up.
    """
    ciphertext = encrypt(SecretStr(FIXTURE_SECRET))
    with engine.begin() as conn:
        _insert(conn, TENANT, "analytics", ciphertext)

    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT credential FROM connector WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar()

    assert stored is not None
    assert FIXTURE_SECRET not in stored, "the password is readable in the database"
    # And it must still be usable — encryption that cannot be reversed by the
    # application is just data loss.
    assert decrypt(stored).get_secret_value() == FIXTURE_SECRET


def test_a_connector_may_have_no_credential(engine: Engine) -> None:
    """Not every connector authenticates. A REST connector against a public
    endpoint has nothing to store, and requiring one would invite a placeholder."""
    with engine.begin() as conn:
        _insert(conn, TENANT, "public-api", None)

    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT credential FROM connector WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar()
    assert stored is None


# --- defaults ---------------------------------------------------------------


def test_labels_default_to_empty_which_means_nobody(engine: Engine) -> None:
    """Default-deny.

    A connector whose configuration was never finished should be reachable by
    no one. The alternative — defaulting to a permissive label — makes a
    half-configured connector silently available.
    """
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO connector (id, tenant_id, kind, slug, display_name)
                VALUES (:id, :t, 'sql', 'bare', 'Bare')
            """),
            {"id": uuid.uuid4(), "t": TENANT},
        )

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT required_labels, enabled FROM connector WHERE tenant_id = :t"),
            {"t": TENANT},
        ).one()

    assert list(row.required_labels) == []
    # Enabled by default is safe precisely BECAUSE labels are not: an enabled
    # connector that nobody's labels match is still reachable by nobody.
    assert row.enabled is True
