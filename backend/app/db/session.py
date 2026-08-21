"""Database and cache connections.

Cross-cutting module: anything may import this; it imports nothing but core.
"""

from collections.abc import Iterator

import redis
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# A health check must fail fast, not hang. Without an explicit connect timeout the
# driver waits on the OS default (tens of seconds), which turns a down database into
# a hung request instead of a clean 503.
CONNECT_TIMEOUT_SECONDS = 3

# Ceilings on how long a query, and a wait for a row lock, may take.
#
# Postgres defaults both to 0 — unlimited — so a `FOR UPDATE` blocked behind a
# stuck transaction waits forever, and nothing anywhere in the stack interrupts
# it. The SQL connector has always set a statement_timeout for generated
# queries; the application's own connection never did, which is the more
# dangerous omission because it affects every request rather than one tool.
#
# `lock_timeout` is the shorter of the two on purpose: waiting seconds for a
# lock means someone else is holding it, and failing fast with a readable error
# is better than a request that hangs until a client gives up.
STATEMENT_TIMEOUT_MS = 30_000
LOCK_TIMEOUT_MS = 10_000

_TIMEOUT_OPTIONS = f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c lock_timeout={LOCK_TIMEOUT_MS}"

# app_database_url, not database_url. The owner role bypasses RLS unconditionally,
# so using it at runtime would silently disable every tenant isolation policy.
# Migrations use the owner; the request path never does.
engine: Engine = create_engine(
    settings.app_database_url,
    # Verify a pooled connection is alive before handing it out. Without this,
    # a connection dropped by a container restart surfaces as a request error.
    pool_pre_ping=True,
    connect_args={
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        # Applied per connection rather than per session: a SET LOCAL would
        # have to be reissued in every transaction, and the one that forgets is
        # the one that hangs.
        "options": _TIMEOUT_OPTIONS,
    },
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# Owner connection: bypasses RLS. Reserved for administrative work that is
# legitimately outside any one tenant's context — creating tenants, migrations,
# and test fixtures. It must never serve a request.
owner_engine: Engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    connect_args={
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "options": _TIMEOUT_OPTIONS,
    },
)

redis_client: redis.Redis = redis.Redis.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
    socket_timeout=CONNECT_TIMEOUT_SECONDS,
)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session that always closes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
