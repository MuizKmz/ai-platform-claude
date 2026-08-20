"""Durable run state.

A run makes several model calls and several upstream requests over tens of
seconds. Without a checkpoint, a worker restart mid-run loses all of it —
including the money already spent — and the caller sees nothing but a dropped
connection.

**The isolation gap, stated plainly.** LangGraph's checkpoint tables key on
`thread_id` and carry no `tenant_id`, so Row-Level Security does not protect
them. That is the one place in this system where tenant isolation is not
enforced by the database, and it is handled at the boundary instead:

  - Thread ids are server-generated UUIDs, never accepted from a request.
  - `agent_run` — our own table, with `tenant_id` and an RLS policy — records
    which tenant owns which thread.
  - Resuming a run resolves ownership from `agent_run` BEFORE touching a
    checkpoint.

So a caller who guessed a thread id would still have to pass a tenant check
that reads from a table the database is protecting. Worth writing down because
"the database enforces it" is true everywhere else here, and someone will
reasonably assume it is true of this too.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

_checkpointer: Any = None
_setup_done = False


def get_checkpointer() -> Any:
    """A process-wide Postgres checkpointer, or None if unavailable.

    Returning None rather than raising is deliberate: an agent that cannot
    checkpoint is degraded, not broken. Losing durability is worth less than
    losing the ability to answer questions, and the log line says which one
    happened.
    """
    global _checkpointer, _setup_done

    if _checkpointer is not None:
        return _checkpointer

    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool
    except ImportError:
        logger.warning("langgraph postgres checkpointer not installed; runs are not durable")
        return None

    try:
        # psycopg3 connection string: the checkpointer manages raw connections
        # rather than going through SQLAlchemy, so the +psycopg driver prefix
        # has to come off.
        dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
        pool = ConnectionPool(
            conninfo=dsn,
            max_size=4,
            # autocommit because the checkpointer manages its own transactions;
            # an outer transaction would hold a connection open for the whole
            # run rather than per write.
            kwargs={"autocommit": True},
            open=True,
        )
        saver = PostgresSaver(pool)  # type: ignore[arg-type]

        if not _setup_done:
            # Creates LangGraph's own tables. Idempotent, and deliberately not
            # an Alembic migration: the schema belongs to the framework, and
            # owning a copy of it in our migration history would mean
            # maintaining someone else's DDL.
            saver.setup()
            _setup_done = True

        _checkpointer = saver
    except Exception as exc:  # noqa: BLE001 — degraded is acceptable, crashed is not
        logger.warning(
            "checkpointer unavailable (%s); runs will not survive a restart",
            type(exc).__name__,
        )
        return None

    return _checkpointer


def reset_checkpointer() -> None:
    """Drop the cached checkpointer. For tests."""
    global _checkpointer, _setup_done
    _checkpointer = None
    _setup_done = False
