"""Rate limiting.

Introduced in Phase 6 for one endpoint — "Test Connection" — because that
endpoint is a network probe and the roadmap requires it be treated as one.
Phase 8 builds the general per-tenant/per-user/per-tool limits; this is the
narrow piece that cannot wait, since shipping an unthrottled probe and adding
the throttle two phases later means the probe is unthrottled in between.

**Fixed window, not a token bucket.** A window counter is one INCR and one
EXPIRE, it is correct under concurrency without a Lua script, and its known
weakness — up to 2x the limit across a window boundary — does not matter for a
control whose purpose is to stop a user enumerating a network at speed. A
sliding window would be more precise and more machinery than the threat needs.

**Fails closed.** If Redis is unreachable the limiter denies rather than allows.
That is the opposite of the usual availability tradeoff and deliberate here: the
thing being limited is an SSRF primitive, and "the rate limiter is down" is
exactly when someone would want it to be.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import cast

import redis

from app.db.session import redis_client

logger = logging.getLogger(__name__)


class RateLimitExceededError(Exception):
    """The caller has exhausted its allowance for the current window."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Rate limit exceeded.")


@dataclass(frozen=True)
class RateLimit:
    """How many operations are allowed per window."""

    limit: int
    window_seconds: int


def check_rate_limit(
    key: str,
    policy: RateLimit,
    *,
    client: redis.Redis | None = None,
) -> int:
    """Consume one unit of allowance, or raise.

    Returns how many remain in the window, which is worth surfacing to a caller
    so a UI can warn before the limit is hit rather than after.

    The key must already be namespaced by whatever the limit is per — a tenant,
    a user, both. Building the key here would mean this function deciding the
    policy's scope, and the scope belongs with the endpoint that knows what it
    is protecting.
    """
    conn = client if client is not None else redis_client
    full_key = f"ratelimit:{key}"

    try:
        pipe = conn.pipeline()
        pipe.incr(full_key)
        # Set the TTL on every request rather than only when the counter is
        # created. A key that somehow lost its expiry would otherwise block the
        # caller forever, and re-setting it costs nothing.
        pipe.expire(full_key, policy.window_seconds)
        count, _ = pipe.execute()
    except redis.RedisError as exc:
        # Fail closed. See the module docstring: the limited operation is a
        # network probe, and an outage is precisely when an attacker benefits
        # from the limiter being open.
        logger.error("rate limiter unavailable, denying: %s", type(exc).__name__)
        raise RateLimitExceededError(policy.window_seconds) from exc

    used = int(count)
    if used > policy.limit:
        # cast: the sync client returns an int, but the shared stubs describe
        # the async client's Awaitable too.
        ttl = cast(int, conn.ttl(full_key))
        raise RateLimitExceededError(ttl if ttl > 0 else policy.window_seconds)

    return policy.limit - used
