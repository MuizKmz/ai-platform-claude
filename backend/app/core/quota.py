"""Per-tenant spend ceilings.

The agent's `RunLimits` cap one run. This caps a tenant across all runs, which
is a different failure: a hundred well-behaved requests can exhaust a budget
without any one of them misbehaving, and `max_cost_usd=0.50` per run says
nothing about the thousandth run of the day.

**Redis, not Postgres.** A quota is read and incremented on every LLM call, and
doing that in the request's transaction would either serialise every request on
one row or produce a count that rolls back with the request that spent the
money. The cost was still incurred — the provider was called — so the counter
must survive a rollback. Redis is outside the transaction, which is the
property needed here.

**Fails OPEN.** If Redis is unreachable, requests proceed. This is the opposite
of `ratelimit.py`, deliberately, and the difference is worth stating: the rate
limiter guards an SSRF primitive, where an outage is exactly when an attacker
benefits from it being open. A quota guards a *bill*. Denying every request in
the tenant because a cache is down turns a billing control into an outage, and
the money at stake in one Redis-outage window is far smaller than the cost of
the platform being unavailable. The breach is logged loudly.

Windows are UTC calendar days. Not a rolling window: "you have spent $4.80 of
$5.00 today" is something a person can reason about and predict the reset of,
where a rolling 24-hour figure moves for reasons nobody can see.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import redis

from app.core.config import settings
from app.db.session import redis_client

logger = logging.getLogger(__name__)

# Stored as integer micro-dollars. Redis INCRBY is atomic on integers; INCRBYFLOAT
# exists but accumulates binary-float error across thousands of small additions,
# and a quota that drifts is a quota nobody trusts.
MICRO = 1_000_000


class QuotaExceededError(Exception):
    """A tenant has spent its allowance for the current window."""

    def __init__(self, spent_usd: float, limit_usd: float, resets_at: datetime) -> None:
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        self.resets_at = resets_at
        super().__init__(
            f"Daily budget of ${limit_usd:.2f} exhausted "
            f"(spent ${spent_usd:.4f}). Resets at {resets_at:%Y-%m-%d %H:%M} UTC."
        )


@dataclass(frozen=True)
class QuotaStatus:
    spent_usd: float
    limit_usd: float
    resets_at: datetime

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.limit_usd


def _window_key(tenant_id: uuid.UUID, now: datetime | None = None) -> str:
    day = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    return f"quota:{tenant_id}:{day}"


def _resets_at(now: datetime | None = None) -> datetime:
    """Midnight UTC after `now`. Computed by adding a day to the current day's
    start rather than by incrementing the day number, which breaks on the 31st."""
    moment = now or datetime.now(UTC)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(days=1)


def check_quota(
    tenant_id: uuid.UUID,
    *,
    client: redis.Redis | None = None,
    now: datetime | None = None,
) -> QuotaStatus:
    """Read spend so far. Raises if the ceiling is already reached.

    Called BEFORE an expensive operation. The check and the later `record_spend`
    are deliberately not atomic: making them so would mean holding a lock across
    a model call that can take thirty seconds. A tenant can therefore overshoot
    by at most the cost of the requests already in flight, which is cents.
    """
    status = quota_status(tenant_id, client=client, now=now)
    if status.exhausted:
        raise QuotaExceededError(status.spent_usd, status.limit_usd, status.resets_at)
    return status


def quota_status(
    tenant_id: uuid.UUID,
    *,
    client: redis.Redis | None = None,
    now: datetime | None = None,
) -> QuotaStatus:
    """Current spend without enforcing it. For display."""
    limit = settings.tenant_daily_budget_usd
    conn = client if client is not None else redis_client

    try:
        raw = conn.get(_window_key(tenant_id, now))
    except redis.RedisError as exc:
        # Fails OPEN. See the module docstring: this guards a bill, and denying
        # a tenant because a cache is down converts a billing control into an
        # outage. Logged at error so it is visible rather than silent.
        logger.error("quota unavailable, allowing: %s", type(exc).__name__)
        return QuotaStatus(spent_usd=0.0, limit_usd=limit, resets_at=_resets_at(now))

    # cast: the sync client returns bytes|None, but the shared stubs also
    # describe the async client's Awaitable.
    spent_micro = int(cast(bytes, raw)) if raw else 0
    return QuotaStatus(spent_usd=spent_micro / MICRO, limit_usd=limit, resets_at=_resets_at(now))


def record_spend(
    tenant_id: uuid.UUID,
    cost_usd: float,
    *,
    client: redis.Redis | None = None,
    now: datetime | None = None,
) -> None:
    """Add to the window's total.

    Called AFTER the money is spent, and never inside the request transaction:
    the provider was billed whether or not the request commits, so a counter
    that rolls back would undercount exactly the failures worth counting.
    """
    if cost_usd <= 0:
        return

    conn = client if client is not None else redis_client
    key = _window_key(tenant_id, now)

    try:
        pipe = conn.pipeline()
        pipe.incrby(key, int(round(cost_usd * MICRO)))
        # Two days, not one: a key written at 23:59 must outlive its own window
        # long enough to be read correctly, and an over-long TTL costs nothing.
        pipe.expire(key, 2 * 24 * 60 * 60)
        pipe.execute()
    except redis.RedisError as exc:
        # Spend not recorded. The request already happened and cannot be undone,
        # so the only options are to lose the count or to fail after the fact —
        # losing the count is the lesser harm, and it must be loud.
        logger.error(
            "spend of $%.6f for tenant %s was NOT recorded: %s",
            cost_usd,
            tenant_id,
            type(exc).__name__,
        )
