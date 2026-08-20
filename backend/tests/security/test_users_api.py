"""The users API.

`allowed_labels` is the permission set retrieval filters on, tool authorization
checks, and connectors gate access with. Editing it is granting or revoking
access, so most of this file is about the ways that can go wrong:

  - a label nobody uses, granted by typo, that silently authorizes nothing
  - an admin removing their own admin role and being unable to undo it
  - the last admin being deleted, leaving the tenant with no way back in

The lockout cases matter more than they look. There is no password reset here
and no support desk — recovery from "no admins left" means someone with
database access writing a row by hand.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.core.config import settings
from app.core.security import issue_token
from app.main import app


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

TENANT = uuid.UUID("5e110000-0000-0000-0000-00000000005e")
OTHER = uuid.UUID("5e110000-0000-0000-0000-0000000000ff")
ADMIN_ID = uuid.UUID("5e110000-0000-0000-0000-0000000000a1")


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
            conn.execute(text("DELETE FROM app_user WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM connector WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM chunk WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM document WHERE tenant_id IN (:a, :b)"), params)
            conn.execute(text("DELETE FROM tenant WHERE id IN (:a, :b)"), params)

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO tenant (id, slug, name) VALUES
                  (:a, 'users-test', 'Users Test'), (:b, 'users-other', 'Users Other')
            """),
            {"a": TENANT, "b": OTHER},
        )
        # The acting admin must exist as a row: the self-lockout guards compare
        # against principal.user_id.
        conn.execute(
            text("""
                INSERT INTO app_user (id, tenant_id, email, roles, allowed_labels)
                VALUES (:id, :t, 'admin@users.test', ARRAY['admin'], ARRAY['public'])
            """),
            {"id": ADMIN_ID, "t": TENANT},
        )
        # A document carrying labels, so label validation has something to
        # consider "in use".
        conn.execute(
            text("""
                INSERT INTO document (id, tenant_id, title, source_path, content_hash, labels)
                VALUES (:id, :t, 'doc', '/doc', :hash, ARRAY['public', 'finance'])
            """),
            {"id": uuid.uuid4(), "t": TENANT, "hash": uuid.uuid4().hex},
        )
    yield
    _wipe()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _headers(
    *, admin: bool = True, tenant: uuid.UUID = TENANT, user_id: uuid.UUID = ADMIN_ID
) -> dict[str, str]:
    token = issue_token(
        tenant_id=tenant,
        user_id=user_id,
        email="admin@users.test" if admin else "reader@users.test",
        roles=("admin",) if admin else ("reader",),
        allowed_labels=("public",),
    )
    return {"Authorization": f"Bearer {token}"}


def _create(client: TestClient, email: str, **kwargs: object) -> dict:
    body = {"email": email, "roles": ["reader"], "allowed_labels": [], **kwargs}
    response = client.post("/v1/users", json=body, headers=_headers())
    assert response.status_code == 201, response.text
    return response.json()


# --- authorization -----------------------------------------------------------


def test_managing_users_requires_admin(client: TestClient) -> None:
    reader = _headers(admin=False)
    assert client.get("/v1/users", headers=reader).status_code == 403
    assert client.post("/v1/users", json={"email": "x@y.com"}, headers=reader).status_code == 403


def test_users_require_authentication(client: TestClient) -> None:
    assert client.get("/v1/users").status_code == 401


def test_another_tenants_users_are_not_visible(client: TestClient) -> None:
    created = _create(client, "visible@users.test")
    other = client.get(f"/v1/users/{created['id']}", headers=_headers(tenant=OTHER))
    assert other.status_code == 404


def test_tenant_is_server_derived(client: TestClient, engine: Engine) -> None:
    """Invariant #1: a tenant_id in the body is ignored."""
    response = client.post(
        "/v1/users",
        json={"email": "sneaky@users.test", "tenant_id": str(OTHER)},
        headers=_headers(),
    )
    assert response.status_code == 201

    with engine.connect() as conn:
        owner = conn.execute(
            text("SELECT tenant_id FROM app_user WHERE id = :id"),
            {"id": response.json()["id"]},
        ).scalar()
    assert owner == TENANT


# --- labels are the permission set -------------------------------------------


def test_a_label_nothing_uses_is_rejected(client: TestClient) -> None:
    """The typo case.

    Granting `finanace` authorizes nothing while looking exactly like success,
    and the person reports "I still cannot see the finance documents" days
    later. Failing now, with the list of real labels, is far kinder.
    """
    response = client.post(
        "/v1/users",
        json={"email": "typo@users.test", "allowed_labels": ["finanace"]},
        headers=_headers(),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "finanace" in detail
    # And it says what IS available, so the fix is obvious.
    assert "finance" in detail


def test_a_label_in_use_is_accepted(client: TestClient) -> None:
    created = _create(client, "granted@users.test", allowed_labels=["finance"])
    assert created["allowed_labels"] == ["finance"]


def test_a_connectors_label_counts_as_in_use(client: TestClient, engine: Engine) -> None:
    """Labels gate connectors as well as documents, so both are sources of
    truth for what exists."""
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO connector (id, tenant_id, kind, slug, display_name, required_labels)
                VALUES (:id, :t, 'sql', 'warehouse', 'Warehouse', ARRAY['analytics'])
            """),
            {"id": uuid.uuid4(), "t": TENANT},
        )

    created = _create(client, "analyst@users.test", allowed_labels=["analytics"])
    assert created["allowed_labels"] == ["analytics"]


def test_an_unknown_role_is_rejected(client: TestClient) -> None:
    """An arbitrary string is accepted by the array column and then matches
    nothing — a grant that is not one."""
    response = client.post(
        "/v1/users",
        json={"email": "wat@users.test", "roles": ["superuser"]},
        headers=_headers(),
    )
    assert response.status_code == 422
    assert "superuser" in response.json()["detail"]


def test_known_labels_are_listed_for_the_ui(client: TestClient) -> None:
    """So a console can offer labels rather than invite typing them."""
    response = client.get("/v1/users/labels", headers=_headers())
    assert response.status_code == 200
    assert set(response.json()) >= {"public", "finance"}


# --- lockout guards ----------------------------------------------------------


def test_an_admin_cannot_remove_their_own_admin_role(client: TestClient) -> None:
    """They could not undo it, and if they are the last admin nobody can."""
    response = client.patch(f"/v1/users/{ADMIN_ID}", json={"roles": ["reader"]}, headers=_headers())
    assert response.status_code == 409
    assert "your own" in response.json()["detail"].lower()


def test_the_last_admin_cannot_be_demoted(client: TestClient, engine: Engine) -> None:
    """Asserted from the other direction: a DIFFERENT admin doing the demoting.

    Without a second admin in the tenant this would pass for the wrong reason —
    the self-check would fire first and the last-admin rule would never run.
    """
    second_admin = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO app_user (id, tenant_id, email, roles, allowed_labels)
                VALUES (:id, :t, 'second@users.test', ARRAY['admin'], ARRAY['public'])
            """),
            {"id": second_admin, "t": TENANT},
        )
        # Remove the fixture admin so `second` is the only one left, then act
        # as a THIRD identity so the self-check cannot be what refuses.
        conn.execute(text("DELETE FROM app_user WHERE id = :id"), {"id": ADMIN_ID})

    response = client.patch(
        f"/v1/users/{second_admin}",
        json={"roles": ["reader"]},
        headers=_headers(user_id=uuid.uuid4()),
    )
    assert response.status_code == 409
    assert "last admin" in response.json()["detail"].lower()


def test_an_admin_can_be_demoted_when_another_remains(client: TestClient, engine: Engine) -> None:
    """The complement, so the guard is not simply 'admins are immutable'."""
    spare = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO app_user (id, tenant_id, email, roles, allowed_labels)
                VALUES (:id, :t, 'spare@users.test', ARRAY['admin'], ARRAY['public'])
            """),
            {"id": spare, "t": TENANT},
        )

    response = client.patch(f"/v1/users/{spare}", json={"roles": ["reader"]}, headers=_headers())
    assert response.status_code == 200
    assert response.json()["roles"] == ["reader"]


def test_an_admin_cannot_delete_themselves(client: TestClient) -> None:
    response = client.delete(f"/v1/users/{ADMIN_ID}", headers=_headers())
    assert response.status_code == 409


def test_the_last_admin_cannot_be_deleted(client: TestClient, engine: Engine) -> None:
    second_admin = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO app_user (id, tenant_id, email, roles, allowed_labels)
                VALUES (:id, :t, 'solo@users.test', ARRAY['admin'], ARRAY['public'])
            """),
            {"id": second_admin, "t": TENANT},
        )
        conn.execute(text("DELETE FROM app_user WHERE id = :id"), {"id": ADMIN_ID})

    response = client.delete(f"/v1/users/{second_admin}", headers=_headers(user_id=uuid.uuid4()))
    assert response.status_code == 409
    assert "last admin" in response.json()["detail"].lower()


# --- ordinary operations -----------------------------------------------------


def test_labels_can_be_granted_and_revoked(client: TestClient) -> None:
    created = _create(client, "changing@users.test")
    assert created["allowed_labels"] == []

    granted = client.patch(
        f"/v1/users/{created['id']}",
        json={"allowed_labels": ["finance", "public"]},
        headers=_headers(),
    )
    assert sorted(granted.json()["allowed_labels"]) == ["finance", "public"]

    revoked = client.patch(
        f"/v1/users/{created['id']}", json={"allowed_labels": []}, headers=_headers()
    )
    assert revoked.json()["allowed_labels"] == []


def test_duplicate_email_in_one_tenant_is_a_conflict(client: TestClient) -> None:
    _create(client, "dupe@users.test")
    response = client.post("/v1/users", json={"email": "dupe@users.test"}, headers=_headers())
    assert response.status_code == 409


def test_the_same_email_is_free_in_another_tenant(client: TestClient) -> None:
    """Two tenants may both employ alice@example.com; they are different
    people, and rejecting the second would disclose the first."""
    _create(client, "alice@example.com")
    other = client.post(
        "/v1/users", json={"email": "alice@example.com"}, headers=_headers(tenant=OTHER)
    )
    assert other.status_code == 201


def test_a_malformed_email_is_rejected(client: TestClient) -> None:
    response = client.post("/v1/users", json={"email": "not-an-email"}, headers=_headers())
    assert response.status_code == 422


def test_a_reader_can_be_deleted(client: TestClient) -> None:
    created = _create(client, "temp@users.test")
    assert client.delete(f"/v1/users/{created['id']}", headers=_headers()).status_code == 204
    assert client.get(f"/v1/users/{created['id']}", headers=_headers()).status_code == 404


# --- labels that stopped being used ------------------------------------------


def test_a_label_that_is_no_longer_used_can_be_carried_forward(
    client: TestClient, engine: Engine
) -> None:
    """Editing a user must not require revoking a stale grant.

    Found in real data: two users held `finance` while no document carried it.
    Validating the whole set made them uneditable — every save failed until the
    admin dropped the label, which is a silent revocation wearing a validation
    error's clothes. Only additions are checked.
    """
    created = _create(client, "holder@users.test", allowed_labels=["finance"])

    # The label stops being used: its only document goes away.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE document SET labels = ARRAY['public'] WHERE tenant_id = :t"),
            {"t": TENANT},
        )

    unchanged = client.patch(
        f"/v1/users/{created['id']}",
        json={"allowed_labels": ["finance"]},
        headers=_headers(),
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["allowed_labels"] == ["finance"]


def test_a_new_bad_label_is_still_rejected_on_update(client: TestClient, engine: Engine) -> None:
    """The complement. Carrying a stale grant forward is allowed; inventing a
    new one is not, or the typo guard would be worthless on every edit."""
    created = _create(client, "adder@users.test", allowed_labels=["finance"])

    response = client.patch(
        f"/v1/users/{created['id']}",
        json={"allowed_labels": ["finance", "marketting"]},
        headers=_headers(),
    )
    assert response.status_code == 422
    assert "marketting" in response.json()["detail"]


def test_a_stale_label_can_still_be_removed(client: TestClient, engine: Engine) -> None:
    """Deliberate revocation must remain possible — the point is that it is a
    choice rather than a side effect of saving."""
    created = _create(client, "dropper@users.test", allowed_labels=["finance", "public"])

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE document SET labels = ARRAY['public'] WHERE tenant_id = :t"),
            {"t": TENANT},
        )

    response = client.patch(
        f"/v1/users/{created['id']}",
        json={"allowed_labels": ["public"]},
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["allowed_labels"] == ["public"]
