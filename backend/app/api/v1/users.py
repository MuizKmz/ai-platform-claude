"""Users, roles, and labels.

Admin-only, and more consequential than it looks. `allowed_labels` is not
metadata — it is the permission set that retrieval filters on before ranking,
that tool authorization checks at every invocation, and that decides which
connectors a person can reach. Editing this list is granting or revoking access,
so it is treated as a privilege operation rather than a profile edit.

Three guards are worth naming, because each closes a way an admin could escalate
or lock everyone out:

  - **A label must exist somewhere before it can be granted.** Not a security
    boundary — the label is meaningless until something requires it — but it
    turns a typo from a silent no-op into a visible error. `finanace` grants
    access to nothing and looks like it worked.

  - **You cannot remove your own admin role.** An admin who does so cannot undo
    it, and if they are the last admin the tenant has no way back in without
    database access.

  - **The last admin cannot be deleted or demoted.** Same reasoning, from the
    other direction.

Tenancy is server-derived throughout. A user is created in the caller's tenant,
never one named in the body.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.api.deps import CurrentPrincipal, TenantDB
from app.core.security import Principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["users"])

# Roles the platform understands. An arbitrary string would be accepted by the
# array column and then match nothing, which looks like a grant and is not.
KNOWN_ROLES = ("admin", "reader")


# Deliberately loose. Full RFC 5322 validation needs the email-validator
# package, which would be a new dependency and therefore an ADR — for a field
# whose real validation is "can this person receive mail", which no regex
# answers. This rejects the obvious mistakes and nothing more.
_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class UserCreate(BaseModel):
    email: str = Field(pattern=_EMAIL_PATTERN, max_length=320)
    roles: list[str] = Field(default_factory=lambda: ["reader"])
    allowed_labels: list[str] = Field(default_factory=list)


class UserUpdate(BaseModel):
    roles: list[str] | None = None
    allowed_labels: list[str] | None = None


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    roles: list[str]
    allowed_labels: list[str]
    created_at: datetime


def _require_admin(principal: Principal) -> None:
    if not principal.has_role("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managing users requires the admin role.",
        )


def _row_to_out(row: Any) -> UserOut:
    return UserOut(
        id=row.id,
        email=row.email,
        roles=list(row.roles or []),
        allowed_labels=list(row.allowed_labels or []),
        created_at=row.created_at,
    )


_SELECT = "SELECT id, email, roles, allowed_labels, created_at FROM app_user"


def _known_labels(db: Any) -> set[str]:
    """Labels that exist somewhere in this tenant.

    Union of what documents carry and what connectors require, because those
    are the two things a label can currently gate. RLS scopes both.
    """
    rows = db.execute(
        text("""
            SELECT DISTINCT unnest(labels) AS label FROM document
            UNION
            SELECT DISTINCT unnest(required_labels) AS label FROM connector
        """)
    ).all()
    return {r.label for r in rows if r.label}


def _validate_roles(roles: list[str]) -> None:
    unknown = sorted(set(roles) - set(KNOWN_ROLES))
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown role(s): {unknown}. Known roles are {list(KNOWN_ROLES)}.",
        )


def _validate_labels(db: Any, labels: list[str], *, already_held: list[str] | None = None) -> None:
    """Reject a label nothing uses — but only one being newly granted.

    A typo'd label grants access to nothing while looking like it worked, and
    the person it was granted to reports 'I still cannot see the finance docs'
    days later. Failing now, naming the labels that do exist, is far kinder.

    `already_held` is what stops this from becoming a trap. A label can stop
    being used after it was granted — the document carrying it was deleted, or
    it was a typo nobody caught. Validating the whole set would then make the
    user uneditable: every save fails until the admin drops the label, which is
    a silent revocation disguised as a validation error. Only additions are
    checked, so an existing grant can be carried forward or removed
    deliberately, and a new bad one is still refused.
    """
    if not labels:
        return
    additions = set(labels) - set(already_held or [])
    if not additions:
        return
    known = _known_labels(db)
    unknown = sorted(additions - known)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"No document or connector uses label(s): {unknown}. "
            f"Labels currently in use: {sorted(known) or 'none'}.",
        )


def _admin_count(db: Any) -> int:
    return int(
        db.execute(text("SELECT count(*) FROM app_user WHERE 'admin' = ANY(roles)")).scalar() or 0
    )


@router.get("/users", response_model=list[UserOut])
def list_users(principal: CurrentPrincipal, db: TenantDB) -> list[UserOut]:
    """Users in this tenant. RLS supplies the tenant predicate."""
    _require_admin(principal)
    rows = db.execute(text(f"{_SELECT} ORDER BY created_at")).all()
    return [_row_to_out(r) for r in rows]


@router.get("/users/labels", response_model=list[str])
def list_known_labels(principal: CurrentPrincipal, db: TenantDB) -> list[str]:
    """Labels in use, so a UI can offer them rather than invite typing.

    Deliberately placed above /users/{user_id} — FastAPI matches in declaration
    order, and 'labels' would otherwise be parsed as a UUID and 422.
    """
    _require_admin(principal)
    return sorted(_known_labels(db))


@router.get("/users/{user_id}", response_model=UserOut)
def get_user(user_id: uuid.UUID, principal: CurrentPrincipal, db: TenantDB) -> UserOut:
    _require_admin(principal)
    row = db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": user_id}).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user.")
    return _row_to_out(row)


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, principal: CurrentPrincipal, db: TenantDB) -> UserOut:
    _require_admin(principal)
    _validate_roles(body.roles)
    _validate_labels(db, body.allowed_labels)

    exists = db.execute(
        text("SELECT 1 FROM app_user WHERE email = :email"), {"email": body.email}
    ).first()
    if exists:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A user with email {body.email} already exists."
        )

    new_id = uuid.uuid4()
    db.execute(
        text("""
            INSERT INTO app_user (id, tenant_id, email, roles, allowed_labels)
            VALUES (:id, :tenant, :email, :roles, :labels)
        """),
        {
            "id": new_id,
            # Server-derived. Invariant #1.
            "tenant": principal.tenant_id,
            "email": str(body.email),
            "roles": body.roles,
            "labels": body.allowed_labels,
        },
    )

    # Logged because granting access is the kind of change someone asks about
    # later. Labels are included: they ARE the grant.
    logger.info(
        "user created: tenant=%s email=%s roles=%s labels=%s by=%s",
        principal.tenant_id,
        body.email,
        body.roles,
        body.allowed_labels,
        principal.email,
    )

    row = db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": new_id}).one()
    return _row_to_out(row)


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    principal: CurrentPrincipal,
    db: TenantDB,
) -> UserOut:
    """Change roles or labels.

    Both are privilege changes. The guards below stop an admin locking
    themselves — or the whole tenant — out.
    """
    _require_admin(principal)

    row = db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": user_id}).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user.")

    updates: dict[str, Any] = {}

    if body.roles is not None:
        _validate_roles(body.roles)
        losing_admin = "admin" in (row.roles or []) and "admin" not in body.roles

        if losing_admin and row.id == principal.user_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "You cannot remove your own admin role. Ask another admin to do it.",
            )
        if losing_admin and _admin_count(db) <= 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This is the last admin. Promote someone else first.",
            )
        updates["roles"] = body.roles

    if body.allowed_labels is not None:
        _validate_labels(db, body.allowed_labels, already_held=list(row.allowed_labels or []))
        updates["allowed_labels"] = body.allowed_labels

    if not updates:
        return _row_to_out(row)

    # Assignments come from a fixed allowlist, never from caller-supplied keys.
    assignments = ", ".join(_ASSIGNMENTS[col] for col in updates)
    statement = f"UPDATE app_user SET {assignments} WHERE id = :id"  # noqa: S608
    db.execute(text(statement), {**updates, "id": user_id})

    logger.info(
        "user updated: tenant=%s email=%s changes=%s by=%s",
        principal.tenant_id,
        row.email,
        {k: v for k, v in updates.items()},
        principal.email,
    )

    updated = db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": user_id}).one()
    return _row_to_out(updated)


# The only columns this endpoint may write, and the exact SQL for each.
_ASSIGNMENTS = {
    "roles": "roles = :roles",
    "allowed_labels": "allowed_labels = :allowed_labels",
}


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: uuid.UUID, principal: CurrentPrincipal, db: TenantDB) -> None:
    _require_admin(principal)

    row = db.execute(
        text("SELECT id, email, roles FROM app_user WHERE id = :id"), {"id": user_id}
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user.")

    if row.id == principal.user_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You cannot delete your own account.")
    if "admin" in (row.roles or []) and _admin_count(db) <= 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This is the last admin. Promote someone else first.",
        )

    db.execute(text("DELETE FROM app_user WHERE id = :id"), {"id": user_id})
    logger.info(
        "user deleted: tenant=%s email=%s by=%s",
        principal.tenant_id,
        row.email,
        principal.email,
    )
