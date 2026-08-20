"""MCP resource-server authentication.

The MCP spec (2026-07-28) requires a server acting as an OAuth 2.1 resource
server to validate that a token was issued **specifically for it**, and states
it **MUST NOT accept or transit any other tokens**. Both halves matter, and the
second is the one people skip.

**Audience validation (RFC 8707).** A token addressed to the platform API is not
a token addressed to the MCP server. Accepting one here would mean a token
issued for a browser session doubles as a machine credential for a different
surface, which is the confused-deputy problem the requirement exists to prevent.
So MCP has its own audience, and a token bearing the API's audience is refused
with the same opaque error as a forged one.

**No passthrough.** Whatever token arrives here is used to derive a Principal
and is then dropped. It is never attached to an outbound request — not to a
connector, not to another MCP server, not to the model provider. A gateway that
forwards its caller's token lets that caller reach everything the gateway can
reach, wearing the gateway's trust. `test_mcp_no_token_passthrough` asserts the
token string does not survive into any outbound call.

The Principal derived here is the ordinary one. That is the point of the phase:
the MCP front door produces the same identity object the console does, so the
same `ToolRegistry.invoke` makes the same decision.
"""

from __future__ import annotations

import logging
import uuid

import jwt

from app.core.config import settings
from app.core.security import ALGORITHM, AuthError, Principal

logger = logging.getLogger(__name__)


def mcp_audience() -> str:
    """The audience a token must carry to be usable here.

    Derived from the API audience rather than configured separately, so a
    deployment cannot accidentally make them equal — which would silently
    re-enable the passthrough this module exists to prevent.
    """
    return f"{settings.jwt_audience}-mcp"


def principal_from_mcp_token(token: str) -> Principal:
    """Verify a token issued for the MCP server, or raise AuthError.

    Deliberately a separate function from `principal_from_token` rather than a
    parameter on it. A boolean argument that switches audiences is one call site
    away from being passed wrongly, and this is the check that stops a
    console token being used as a machine credential.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],
            # THE check. A token for the platform API fails here.
            audience=mcp_audience(),
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
        # One opaque error for every failure, as everywhere else. "Wrong
        # audience" and "bad signature" must not be distinguishable, or the
        # response becomes an oracle for what this server accepts.
        raise AuthError("token verification failed") from exc

    try:
        tenant_id = uuid.UUID(claims["https://eaip.dev/tenant_id"])
        user_id = uuid.UUID(claims["sub"])
        email = str(claims["https://eaip.dev/email"])
    except (KeyError, ValueError, TypeError) as exc:
        raise AuthError("token is missing required claims") from exc

    return Principal(
        tenant_id=tenant_id,
        user_id=user_id,
        email=email,
        roles=tuple(claims.get("https://eaip.dev/roles") or ()),
        allowed_labels=tuple(claims.get("https://eaip.dev/labels") or ()),
    )


def protected_resource_metadata() -> dict[str, object]:
    """RFC 9728 Protected Resource Metadata.

    Served at `/.well-known/oauth-protected-resource`. It tells a client which
    authorization server to get a token from and, critically, what audience to
    request — a client that cannot discover the audience will ask for the wrong
    one and be refused, which is correct but unhelpful.
    """
    return {
        "resource": mcp_audience(),
        "authorization_servers": [settings.jwt_issuer],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["tools:read"],
        # Stated explicitly rather than left implied. A client reading this
        # knows the server will not forward its token onward.
        "resource_documentation": f"{settings.jwt_issuer}/docs",
    }
