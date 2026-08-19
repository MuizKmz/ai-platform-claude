"""Health endpoint.

Reports real dependency connectivity, not a hardcoded "ok". A health check that
cannot fail tells you nothing — this one issues an actual query and an actual PING.
"""

import logging
from typing import Literal

import redis
from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.db.session import engine, redis_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["operational"])

Status = Literal["ok", "error"]


class HealthResponse(BaseModel):
    """Non-sensitive health payload. Never include versions, hosts, or credentials."""

    status: Status
    db: Status
    redis: Status


def _check_db() -> Status:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        # Log the detail server-side; never leak connection errors to the caller.
        logger.exception("database health check failed")
        return "error"


def _check_redis() -> Status:
    try:
        redis_client.ping()
        return "ok"
    except (redis.RedisError, OSError):
        logger.exception("redis health check failed")
        return "error"


@router.get("/health", response_model=HealthResponse)
def health(response: Response) -> HealthResponse:
    """Return per-dependency status; 503 if any dependency is down."""
    db_status = _check_db()
    redis_status = _check_redis()

    overall: Status = "ok" if db_status == "ok" and redis_status == "ok" else "error"
    if overall == "error":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(status=overall, db=db_status, redis=redis_status)
