"""Who am I — the smallest possible proof that identity is server-derived.

This endpoint exists so tenancy can be inspected and tested before there is
anything to retrieve. It echoes only what the verified token established.
"""

import uuid

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
