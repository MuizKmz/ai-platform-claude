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

from app.core.config import settings
from app.core.quota import QuotaExceededError, check_quota
from app.core.ratelimit import RateLimit, RateLimitExceededError, check_rate_limit
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


def enforce_llm_limits(principal: Principal) -> None:
    """Rate limit and budget check for endpoints that call a model.

    Both are per tenant rather than per user. A per-user limit is trivially
    multiplied by creating users, and the tenant is who receives the invoice.

    Order matters: the rate limit is consumed first, so a tenant that is over
    budget cannot also spend the platform's time on quota lookups at unlimited
    speed. Both raise before any expensive work begins.
    """
    try:
        check_rate_limit(
            f"llm:{principal.tenant_id}",
            RateLimit(limit=settings.llm_requests_per_minute, window_seconds=60),
        )
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Try again shortly.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc

    try:
        check_quota(principal.tenant_id)
    except QuotaExceededError as exc:
        # 402 rather than 429: this is not "slow down", it is "the budget for
        # today is gone". A caller that retries a 429 will succeed shortly; one
        # that retries this will not, and the status should say so.
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=str(exc),
        ) from exc


def _llm_limited(principal: Annotated[Principal, Depends(get_principal)]) -> None:
    enforce_llm_limits(principal)


# Declare on any endpoint that calls a model. Runs before the handler body.
LLMLimited = Annotated[None, Depends(_llm_limited)]
