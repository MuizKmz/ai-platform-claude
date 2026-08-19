"""Request dependencies: identity, and a tenant-scoped database session.

These two are deliberately coupled. A session that is not bound to a verified
Principal has no tenant context, and a query on it returns zero rows — so it is
impossible to accidentally query "as nobody" and get everything.
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import AuthError, Principal, principal_from_token
from app.db.session import SessionLocal

# auto_error=False so a missing header reaches our handler and produces the same
# opaque 401 as a malformed one. Letting the library answer first would leak the
# difference between "no token" and "bad token".
_bearer = HTTPBearer(auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """Derive the verified identity, or 401.

    This is the ONLY place a Principal is created for a request. Nothing else may
    read the Authorization header or construct a Principal from request data.
    """
    if credentials is None or not credentials.credentials:
        raise _UNAUTHORIZED
    try:
        return principal_from_token(credentials.credentials)
    except AuthError as exc:
        # The cause is logged upstream, never returned. "Which part of your token
        # was wrong" is an oracle for an attacker.
        raise _UNAUTHORIZED from exc


def get_tenant_db(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Iterator[Session]:
    """A session whose transaction carries this request's tenant context.

    `SET LOCAL` is what makes the RLS policies from migration 1fb64d2aaf50 apply.
    LOCAL, not SESSION: the value is scoped to this transaction and is discarded
    on commit or rollback, so a pooled connection can never carry one request's
    tenant into the next request's query.

    The parameter is bound, not interpolated. tenant_id is a UUID from a verified
    token so injection is not the live risk here, but a SET statement built by
    string concatenation is a pattern that gets copied to places where it is.
    """
    db = SessionLocal()
    try:
        db.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(principal.tenant_id)},
        )
        yield db
        # Committing here rather than in each endpoint keeps the tenant context
        # and the work it guarded inside one transaction boundary.
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
TenantDB = Annotated[Session, Depends(get_tenant_db)]
