"""Every tenant-scoped table must have Row-Level Security.

This exists because trace_span did not, and nothing noticed. It carried
tenant_id, was written on every request, and any session could read every
tenant's spans — found by auditing pg_policies against pg_tables during a
consolidation pass, not by a failing test.

The per-table tests elsewhere assert that isolation WORKS on the tables they know
about. This one asserts that no table was FORGOTTEN, which is a different
question and the one that actually failed.

A new tenant table added in a later phase gets this check for free.
"""

from __future__ import annotations

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

# Tables that legitimately have no tenant_id and therefore need no policy.
#
# `tenant` itself is the registry of tenants: scoping it to a tenant would make
# it unreadable during the request that establishes which tenant is calling.
# `alembic_version` is schema metadata.
_EXEMPT = frozenset({"tenant", "alembic_version"})


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


def _tenant_scoped_tables(engine: Engine) -> list[str]:
    """Every public table carrying a tenant_id column."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT c.table_name
                FROM information_schema.columns c
                JOIN information_schema.tables t
                  ON t.table_name = c.table_name AND t.table_schema = c.table_schema
                WHERE c.table_schema = 'public'
                  AND c.column_name = 'tenant_id'
                  AND t.table_type = 'BASE TABLE'
                ORDER BY c.table_name
            """)
        ).scalars()
        return [name for name in rows if name not in _EXEMPT]


def test_every_tenant_scoped_table_has_a_policy(engine: Engine) -> None:
    """A table with tenant_id and no policy is a table nobody is isolating."""
    tables = _tenant_scoped_tables(engine)
    assert tables, "no tenant-scoped tables found — the query is probably wrong"

    with engine.connect() as conn:
        policed = set(
            conn.execute(
                text("SELECT DISTINCT tablename FROM pg_policies WHERE schemaname = 'public'")
            ).scalars()
        )

    missing = sorted(set(tables) - policed)
    assert not missing, (
        f"tenant-scoped tables without an RLS policy: {missing}. "
        "Add one in a migration, or add the table to _EXEMPT with a reason."
    )


def test_rls_is_forced_on_every_tenant_scoped_table(engine: Engine) -> None:
    """ENABLE alone exempts the table owner. FORCE is what closes that."""
    tables = _tenant_scoped_tables(engine)

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE relname = ANY(:names)
            """),
            {"names": tables},
        ).all()

    not_enabled = sorted(r.relname for r in rows if not r.relrowsecurity)
    not_forced = sorted(r.relname for r in rows if not r.relforcerowsecurity)

    assert not not_enabled, f"RLS not enabled on: {not_enabled}"
    assert not not_forced, f"RLS enabled but not FORCED on: {not_forced}"


def test_no_tenant_context_means_no_rows_anywhere(engine: Engine) -> None:
    """The property that matters, asserted across every table at once.

    Reading each table as the application role with no app.tenant_id set must
    return nothing. A table that returns rows here is one where a forgotten
    WHERE clause discloses another tenant's data.
    """
    tables = _tenant_scoped_tables(engine)
    app_engine = create_engine(settings.app_database_url)

    leaking: list[tuple[str, int]] = []
    try:
        with app_engine.connect() as conn:
            for table in tables:
                # Table names come from information_schema, not from user input.
                count = conn.execute(
                    text(f'SELECT count(*) FROM "{table}"')  # noqa: S608
                ).scalar()
                if count:
                    leaking.append((table, count))
    finally:
        app_engine.dispose()

    assert not leaking, (
        f"tables returning rows with no tenant context: {leaking}. "
        "Every one of these discloses data across tenants."
    )
