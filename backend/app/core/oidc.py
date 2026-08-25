"""Verifying tokens issued by a real identity provider.

Phase 11. The platform's own issuer (`core.security.issue_token`) signs with a
symmetric secret, which means the verifier holds the key the issuer holds:
anything able to *check* a token is able to *forge* one. This module replaces
that with RS256 against the provider's published public keys, so the API keeps a
public key only — it can verify an identity and cannot mint one.

See ADR 0009. The Principal is unchanged; only the key source and the claim
names move, which is what `core/security.py` predicted in Phase 1.

**The three rules here are each somebody else's incident:**

1. The algorithm is pinned to RS256 and never read from the token's own header.
   `alg: none` skips verification entirely; `alg: HS256` against an RSA *public*
   key lets an attacker sign with a value they are allowed to read.
2. Audience and issuer are verified, not merely present. The MCP server's
   separate audience (ADR 0007) is only meaningful if the check happens.
3. The key set is cached with a TTL and refreshed on an unknown `kid`. Fetching
   per request makes the IdP a denial-of-service surface; never refreshing
   breaks every login the moment keys rotate.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import jwt
from jwt import PyJWKClient
from jwt import PyJWKClientError as _PyJWKClientError
from jwt.exceptions import PyJWKClientConnectionError as _PyJWKClientConnectionError

from app.core.config import settings
from app.core.security import AuthError, Principal

logger = logging.getLogger(__name__)

# Asymmetric, and a list of exactly one. See rule 1 above.
ALGORITHMS = ["RS256"]

# How long a fetched key set is trusted before it is fetched again. Short enough
# that a rotation heals without a restart, long enough that the IdP is not in
# the path of every request.
JWKS_CACHE_SECONDS = 300

# A provider that is slow is a provider that is down, as far as a request is
# concerned. Bounded so a hung IdP cannot hold a worker open.
JWKS_TIMEOUT_SECONDS = 5.0


class OIDCConfigurationError(Exception):
    """The provider is not configured, or its metadata cannot be read.

    Separate from AuthError: this is the deployment being wrong, not the
    caller's token. It must never reach a caller as a 401, because retrying with
    a different token will not fix it.
    """


@dataclass(frozen=True)
class ClaimMapping:
    """Where identity lives in this provider's tokens.

    Keycloak is configured with mappers that emit these; a different provider
    puts them elsewhere. Keeping the names as data rather than literals is what
    makes ADR 0009's "replacing Keycloak is configuration" claim true.
    """

    tenant_id: str
    email: str
    roles: str
    labels: str


DEFAULT_CLAIMS = ClaimMapping(
    tenant_id=settings.oidc_tenant_claim,
    email=settings.oidc_email_claim,
    roles=settings.oidc_roles_claim,
    labels=settings.oidc_labels_claim,
)


class _JWKSCache:
    """One key-set client per issuer, refreshed on a clock and on a miss.

    PyJWKClient does its own caching, but its lifetime is the object's, so a new
    client per request would fetch per request. Holding one here — behind a lock,
    because FastAPI serves requests on a thread pool — is what makes the cache
    real.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._lock = threading.Lock()
        self._client: PyJWKClient | None = None
        self._fetched_at = 0.0

    def signing_key(self, token: str) -> Any:
        """The key this token says it was signed with.

        On an unknown `kid` the key set is discarded and fetched once more: that
        is the shape of a rotation, and the alternative is every token minted
        after it failing until the TTL happens to lapse.
        """
        try:
            return self._client_now().get_signing_key_from_jwt(token).key
        except (_PyJWKClientConnectionError, URLError) as exc:
            # Ordered before PyJWKClientError, which it subclasses. The provider
            # being unreachable is the deployment's problem, not the caller's,
            # and answering 401 would send an operator hunting a bad token that
            # does not exist.
            raise OIDCConfigurationError(f"identity provider unreachable: {self._url}") from exc
        except _PyJWKClientError:
            with self._lock:
                self._client = None
            try:
                return self._client_now().get_signing_key_from_jwt(token).key
            except (_PyJWKClientConnectionError, URLError) as exc:
                raise OIDCConfigurationError(f"identity provider unreachable: {self._url}") from exc
            except _PyJWKClientError as exc:
                # The kid is genuinely not published, or the token carries no
                # usable one at all. Either way it cannot be verified, and that
                # is a caller problem.
                raise AuthError("no verification key for this token") from exc

    def _client_now(self) -> PyJWKClient:
        with self._lock:
            expired = time.monotonic() - self._fetched_at > JWKS_CACHE_SECONDS
            if self._client is None or expired:
                self._client = PyJWKClient(
                    self._url,
                    cache_keys=True,
                    timeout=JWKS_TIMEOUT_SECONDS,
                )
                self._fetched_at = time.monotonic()
            return self._client

    def clear(self) -> None:
        with self._lock:
            self._client = None
            self._fetched_at = 0.0


_cache: _JWKSCache | None = None
_cache_lock = threading.Lock()


def _jwks() -> _JWKSCache:
    global _cache
    with _cache_lock:
        if not settings.oidc_jwks_url:
            raise OIDCConfigurationError(
                "OIDC_JWKS_URL is not set. The identity provider must be configured "
                "before tokens can be verified."
            )
        if _cache is None or _cache._url != settings.oidc_jwks_url:
            _cache = _JWKSCache(settings.oidc_jwks_url)
        return _cache


def reset_key_cache() -> None:
    """Drop cached keys. For tests and for a deliberate operational refresh."""
    global _cache
    with _cache_lock:
        if _cache is not None:
            _cache.clear()
        _cache = None


def principal_from_oidc_token(token: str, *, audience: str | None = None) -> Principal:
    """Verify a provider-issued token and derive the Principal, or raise.

    Every verification failure collapses to AuthError, exactly as the local
    issuer's path does: the caller is told "no", never which part was wrong.
    A malformed *deployment* raises OIDCConfigurationError instead, because
    answering 401 to a misconfigured JWKS URL would send an operator hunting
    for a bad token that does not exist.
    """
    key = _jwks().signing_key(token)

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=ALGORITHMS,
            audience=audience or settings.jwt_audience,
            issuer=settings.oidc_issuer or settings.jwt_issuer,
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

    return _principal_from_claims(claims, DEFAULT_CLAIMS)


def _principal_from_claims(claims: dict[str, Any], mapping: ClaimMapping) -> Principal:
    """Map provider claims onto the Principal, refusing anything incomplete.

    A signed token missing tenancy is not a usable identity. It is refused here
    rather than defaulted, because a default is how "no tenant" quietly becomes
    "every tenant" three layers further down — the failure security invariant #1
    exists to prevent.
    """
    try:
        tenant_id = uuid.UUID(str(claims[mapping.tenant_id]))
        user_id = uuid.UUID(str(claims["sub"]))
        email = str(claims[mapping.email])
    except (KeyError, ValueError, TypeError) as exc:
        # Logged as a configuration smell, since the usual cause is a realm
        # whose claim mappers were never set up. The caller still gets a 401.
        logger.warning(
            "token verified but identity claims are missing or malformed; "
            "check the provider's claim mappers"
        )
        raise AuthError("token is missing required identity claims") from exc

    return Principal(
        tenant_id=tenant_id,
        user_id=user_id,
        email=email,
        roles=_string_tuple(claims.get(mapping.roles)),
        allowed_labels=_string_tuple(claims.get(mapping.labels)),
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    """Roles and labels, however the provider chose to express them.

    Keycloak nests realm roles under {"roles": [...]}; some providers send a
    space-delimited string; others send a plain list. All three arrive here, and
    a provider that sends something else gets an empty tuple rather than a
    crash — an identity with no roles is safe, an identity that 500s is not.
    """
    if value is None:
        return ()
    if isinstance(value, dict):
        return _string_tuple(value.get("roles"))
    if isinstance(value, str):
        return tuple(part for part in value.split() if part)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value if isinstance(item, str | int | float))
    return ()


def discover_jwks_url(issuer: str) -> str:
    """Read the provider's metadata to find its key set.

    Standard OIDC discovery (RFC 8414). Used by the setup tooling so an operator
    configures one URL instead of two, and never at request time — a request
    that discovers is a request that depends on the IdP being reachable twice.
    """
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        with urlopen(url, timeout=JWKS_TIMEOUT_SECONDS) as response:  # noqa: S310 - operator-supplied issuer, not user input
            import json

            document = json.loads(response.read())
    except (URLError, ValueError, OSError) as exc:
        raise OIDCConfigurationError(f"could not read OIDC metadata from {url}") from exc

    jwks_uri = document.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise OIDCConfigurationError(f"{url} does not publish a jwks_uri")
    return jwks_uri
