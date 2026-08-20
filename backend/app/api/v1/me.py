"""Who am I — the smallest possible proof that identity is server-derived.

This endpoint exists so tenancy can be inspected and tested before there is
anything to retrieve. It echoes only what the verified token established.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentPrincipal

router = APIRouter(prefix="/v1", tags=["identity"])


class MeResponse(BaseModel):
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    roles: list[str]
    allowed_labels: list[str]


@router.get("/me", response_model=MeResponse)
def me(principal: CurrentPrincipal) -> MeResponse:
    """Return the caller's verified identity.

    Note what is absent: this reads no request body, no query parameter, and no
    header other than the verified bearer token.
    """
    return MeResponse(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        email=principal.email,
        roles=list(principal.roles),
        allowed_labels=list(principal.allowed_labels),
    )


class QuotaResponse(BaseModel):
    spent_usd: float
    limit_usd: float
    remaining_usd: float
    exhausted: bool
    resets_at: datetime


@router.get("/me/quota", response_model=QuotaResponse)
def my_quota(principal: CurrentPrincipal) -> QuotaResponse:
    """This tenant's spend against its daily budget.

    Not admin-only. Everyone who can spend the budget should be able to see how
    much of it is left — a limit people cannot observe is one they discover by
    being refused, which is the worst moment to learn about it.

    Read-only: nothing here can change the ceiling. That is configuration.
    """
    from app.core.quota import quota_status

    status = quota_status(principal.tenant_id)
    return QuotaResponse(
        spent_usd=round(status.spent_usd, 6),
        limit_usd=status.limit_usd,
        remaining_usd=round(status.remaining_usd, 6),
        exhausted=status.exhausted,
        resets_at=status.resets_at,
    )
