"""Row-Level Security tests.

These assert a DATABASE guarantee, not application behaviour. Every query here is
raw SQL that deliberately omits a tenant filter — if the application layer were the
only thing enforcing isolation, all of these would fail.

Requires a running database (docker compose up -d) and an applied migration.
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from app.core.config import settings

pytestmark = pytest.mark.security


def _database_available() -> bool:
    """True if a real Postgres is reachable with the configured credentials.

    A short timeout matters: without it a wrong host makes the suite hang rather
    than fail, which is far harder to diagnose.
    """
    try:
        engine = create_engine(settings.app_database_url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


# These tests assert a database guarantee, so without a database they cannot run.
# They skip rather than fail so a fresh clone is not blocked, and CI starts a real
# Postgres service so the skip never happens there.
pytestmark = [
    pytest.mark.security,
    pytest.mark.skipif(
        not _database_available(),
        reason="no database reachable — run `docker compose up -d` and `alembic upgrade head`",
    ),
]

ACME = uuid.UUID("11111111-1111-1111-1111-111111111111")
GLOBEX = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(scope="module")
def owner_engine() -> Iterator[Engine]:
    """Superuser connection. Used ONLY to seed and clean up — it bypasses RLS."""
    engine = create_engine(settings.database_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def app_engine() -> Iterator[Engine]:
    """The role the application actually uses. RLS applies to this one."""
    engine = create_engine(settings.app_database_url)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def seed(owner_engine: Engine) -> Iterator[None]:
    """Two tenants, one identically-titled document each.

    Identical content matters: it removes any chance that isolation appears to work
    merely because the text differs.
    """
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM chunk"))
        conn.execute(text("DELETE FROM document"))
        conn.execute(text("DELETE FROM tenant"))
        conn.execute(
            text("""
                INSERT INTO tenant (id, slug, name) VALUES
                  (:a, 'acme', 'Acme'), (:g, 'globex', 'Globex')
            """),
            {"a": ACME, "g": GLOBEX},
        )
        conn.execute(
            text("""
                INSERT INTO document (id, tenant_id, title, source_path, content_hash, labels)
                VALUES
                  (gen_random_uuid(), :a, 'Quarterly Report', '/q.md', 'hash-a', '{public}'),
                  (gen_random_uuid(), :g, 'Quarterly Report', '/q.md', 'hash-b', '{public}')
            """),
            {"a": ACME, "g": GLOBEX},
        )
    yield
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM document"))
        conn.execute(text("DELETE FROM tenant"))


def test_rls_blocks_missing_tenant_context(app_engine: Engine) -> None:
    """No app.tenant_id set ⇒ zero rows. Default-deny, enforced by Postgres.

    This is the test that proves isolation does not depend on the application
    remembering to filter.
    """
    with app_engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM document")).scalar()

    assert count == 0, "RLS did not deny a query with no tenant context"


def test_cannot_read_other_tenant_rows(app_engine: Engine) -> None:
    """Each tenant sees exactly its own row, on a query with no WHERE clause."""
    for tenant_id, expected_hash in ((ACME, "hash-a"), (GLOBEX, "hash-b")):
        with app_engine.begin() as conn:
            conn.execute(text(f"SET LOCAL app.tenant_id = '{tenant_id}'"))
            rows = conn.execute(text("SELECT content_hash FROM document")).scalars().all()

        assert rows == [expected_hash], f"tenant {tenant_id} saw {rows}"


def test_cannot_forge_another_tenants_id_in_a_filter(app_engine: Engine) -> None:
    """Asking for another tenant's rows explicitly still returns nothing.

    The policy is an AND against every query, so a hostile WHERE cannot widen it.
    """
    with app_engine.begin() as conn:
        conn.execute(text(f"SET LOCAL app.tenant_id = '{ACME}'"))
        rows = (
            conn.execute(
                text("SELECT content_hash FROM document WHERE tenant_id = :other"),
                {"other": GLOBEX},
            )
            .scalars()
            .all()
        )

    assert rows == [], "a tenant retrieved another tenant's rows by asking for them"


def test_app_role_is_not_superuser(app_engine: Engine) -> None:
    """The guarantee above rests entirely on this.

    A superuser bypasses RLS unconditionally — FORCE ROW LEVEL SECURITY cannot
    override rolbypassrls. If this ever becomes true, every isolation test above
    still passes while isolation is silently gone.
    """
    with app_engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()

    assert row.rolsuper is False, "application role is a superuser; RLS is bypassed"
    assert row.rolbypassrls is False, "application role has BYPASSRLS; RLS is bypassed"


def test_rls_is_forced_not_merely_enabled(owner_engine: Engine) -> None:
    """ENABLE alone exempts the table owner. FORCE is what closes that gap."""
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class WHERE relname IN ('document', 'chunk', 'app_user')
            """)
        ).all()

    assert len(rows) == 3
    for r in rows:
        assert r.relrowsecurity, f"RLS not enabled on {r.relname}"
        assert r.relforcerowsecurity, f"RLS not FORCED on {r.relname}"
