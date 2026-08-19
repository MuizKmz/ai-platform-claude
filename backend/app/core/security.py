"""Identity, tokens, and the Principal.

Security invariant #1: tenancy is server-derived. `tenant_id` comes from a
cryptographically verified token and from nowhere else — never a request body, a
query parameter, a header the caller controls, or model output.

The Principal is the only object the rest of the application is allowed to ask
"who is this and what may they see". Nothing downstream reads raw claims.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import settings

# HS256 is symmetric: the same secret signs and verifies. That is correct for
# locally-issued development tokens. Claims are deliberately OIDC-shaped (sub, iss,
# aud, exp, iat) so a real identity provider can replace this issuer without the
# verification path or the Principal changing — only the key source moves to JWKS.
ALGORITHM = "HS256"


class AuthError(Exception):
    """Token missing, malformed, expired, or failing verification.

    Deliberately carries no detail about which. The API turns this into a bare
    401: telling a caller *why* their token failed helps an attacker far more
    than it helps a legitimate client.
    """


@dataclass(frozen=True)
class Principal:
    """The verified identity behind a request.

    Frozen: once derived from a token it must not be mutable. A `Principal` that
    can be edited downstream is a `Principal` whose tenant_id can be edited
    downstream, which is the exact failure invariant #1 exists to prevent.
    """

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    roles: tuple[str, ...] = field(default_factory=tuple)
    allowed_labels: tuple[str, ...] = field(default_factory=tuple)

    def has_role(self, role: str) -> bool:
        return role in self.roles


def issue_token(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    email: str,
    roles: tuple[str, ...] = (),
    allowed_labels: tuple[str, ...] = (),
    expires_in: timedelta | None = None,
) -> str:
    """Mint a development token.

    This is the local stand-in for an identity provider. It exists so the platform
    can be developed and tested end-to-end before an IdP is wired up; it is not an
    authentication system and performs no credential check. Phase 8 replaces it.
    """
    now = datetime.now(UTC)
    ttl = expires_in if expires_in is not None else timedelta(minutes=settings.jwt_ttl_minutes)
    payload: dict[str, Any] = {
        # OIDC-standard claims.
        "sub": str(user_id),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + ttl,
        # Namespaced private claims. An unprefixed "tenant_id" risks colliding with
        # a claim a future IdP emits with different semantics.
        "https://eaip.dev/tenant_id": str(tenant_id),
        "https://eaip.dev/email": email,
        "https://eaip.dev/roles": list(roles),
        "https://eaip.dev/labels": list(allowed_labels),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def principal_from_token(token: str) -> Principal:
    """Verify a token and derive the Principal, or raise AuthError.

    Every failure mode collapses to one exception type on purpose.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            # A list, and never containing "none". Accepting the algorithm named in
            # the token's own header is the classic JWT vulnerability: an attacker
            # sets alg=none and the signature is not checked at all.
            algorithms=[ALGORITHM],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={
                "require": ["exp", "iat", "sub", "iss", "aud"],
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
                "verify_signature": True,
            },
        )
    except jwt.PyJWTError as exc:
        raise AuthError("token verification failed") from exc

    try:
        tenant_id = uuid.UUID(claims["https://eaip.dev/tenant_id"])
        user_id = uuid.UUID(claims["sub"])
        email = str(claims["https://eaip.dev/email"])
    except (KeyError, ValueError, TypeError) as exc:
        # A signed token that is missing tenancy is not a usable identity. Failing
        # here rather than defaulting is what keeps "no tenant" from meaning "all
        # tenants" further down.
        raise AuthError("token is missing required identity claims") from exc

    return Principal(
        tenant_id=tenant_id,
        user_id=user_id,
        email=email,
        roles=tuple(claims.get("https://eaip.dev/roles") or ()),
        allowed_labels=tuple(claims.get("https://eaip.dev/labels") or ()),
    )
