"""The join between a verified Principal and the RLS policies.

test_rls.py proves the database enforces isolation when app.tenant_id is set.
test_auth.py proves the Principal comes only from a verified token. This file
proves the two are actually wired together — that a request's session really
does carry its tenant context, and that the context does not survive into the
next use of a pooled connection.
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from app.api.deps import get_tenant_db
from app.core.config import settings
from app.core.security import Principal


def _database_available() -> bool:
    try:
        engine = create_engine(settings.app_database_url, connect_args={"connect_timeout": 3})
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

TENANT_A = uuid.UUID("aaaa0000-0000-0000-0000-00000000000a")
TENANT_B = uuid.UUID("bbbb0000-0000-0000-0000-00000000000b")


@pytest.fixture(scope="module")
def owner_engine() -> Iterator[Engine]:
    engine = create_engine(settings.database_url)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def seed(owner_engine: Engine) -> Iterator[None]:
    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM document WHERE tenant_id IN (:a, :b)"), {"a": TENANT_A, "b": TENANT_B}
        )
        conn.execute(
            text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": TENANT_A, "b": TENANT_B}
        )
        conn.execute(
            text("""
                INSERT INTO tenant (id, slug, name) VALUES
                  (:a, 'sess-a', 'Session A'), (:b, 'sess-b', 'Session B')
            """),
            {"a": TENANT_A, "b": TENANT_B},
        )
        conn.execute(
            text("""
                INSERT INTO document (id, tenant_id, title, source_path, content_hash, labels)
                VALUES
                  (gen_random_uuid(), :a, 'A doc', '/a', 'sess-hash-a', '{public}'),
                  (gen_random_uuid(), :b, 'B doc', '/b', 'sess-hash-b', '{public}')
            """),
            {"a": TENANT_A, "b": TENANT_B},
        )
    yield
    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM document WHERE tenant_id IN (:a, :b)"), {"a": TENANT_A, "b": TENANT_B}
        )
        conn.execute(
            text("DELETE FROM tenant WHERE id IN (:a, :b)"), {"a": TENANT_A, "b": TENANT_B}
        )


def _principal(tenant_id: uuid.UUID) -> Principal:
    return Principal(tenant_id=tenant_id, user_id=uuid.uuid4(), email="x@test")


def _titles_for(tenant_id: uuid.UUID) -> list[str]:
    """Drive the real dependency, exactly as a request would."""
    gen = get_tenant_db(_principal(tenant_id))
    db = next(gen)
    try:
        return list(db.execute(text("SELECT title FROM document")).scalars().all())
    finally:
        gen.close()


def test_session_sees_only_the_principals_tenant() -> None:
    """An unfiltered query through the request dependency is still scoped."""
    assert _titles_for(TENANT_A) == ["A doc"]
    assert _titles_for(TENANT_B) == ["B doc"]


def test_tenant_context_does_not_leak_between_sessions() -> None:
    """SET LOCAL is transaction-scoped, so a pooled connection cannot carry one
    request's tenant into the next. Statement-scoped pooling would break this."""
    assert _titles_for(TENANT_A) == ["A doc"]
    assert _titles_for(TENANT_B) == ["B doc"]
    assert _titles_for(TENANT_A) == ["A doc"]


def test_raw_session_without_the_dependency_sees_nothing() -> None:
    """Bypassing get_tenant_db means no tenant context, which means no rows.

    The failure mode is denial, never disclosure.
    """
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(text("SELECT title FROM document")).scalars().all()
    finally:
        db.close()

    assert list(rows) == []
