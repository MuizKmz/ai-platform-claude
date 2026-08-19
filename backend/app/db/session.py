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

engine: Engine = create_engine(
    settings.database_url,
    # Verify a pooled connection is alive before handing it out. Without this,
    # a connection dropped by a container restart surfaces as a request error.
    pool_pre_ping=True,
    connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

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
