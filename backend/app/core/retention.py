"""Data retention: deleting what we no longer need.

Nothing in this system ever deleted anything before this module. Every trace,
every message, every audit row accumulated forever — which is a storage problem
eventually and a liability immediately. Data you do not hold cannot leak, and
data you hold without a reason is pure downside.

**Different tables, different clocks.** The roadmap is explicit about this and
it is worth explaining, because a single retention period would be wrong in
both directions:

    conversations   90 days — what a person asked and was told. The most
                    sensitive thing here, and the least useful once the
                    question is answered.

    traces          30 days — timings and counts for debugging. Nobody debugs
                    a six-week-old latency spike, and traces are the highest
                    volume by far.

    agent runs      90 days — matches conversations, since a run IS a
                    conversation that used tools.

    audit           365 days — who ran what SQL against which connector. This
                    is the compliance record, and it is the one somebody asks
                    for a year later. Longest by a wide margin, deliberately.

**Deletes, not archives.** An archive is a second copy with the same exposure
and none of the attention. If a row is worth keeping it stays in the table.

**Idempotent and interruptible.** Deleting in batches with a cap per run, so a
first run against a large backlog cannot lock a table for minutes. Running it
twice is harmless; running it never is the risk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import text
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from sqlalchemy import CursorResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetentionPolicy:
    """One table, its age column, and how long rows live."""

    table: str
    days: int
    reason: str


# The policy, as data. Adding a table means adding a row here, and
# `test_every_tenant_table_has_a_retention_policy` fails until you do — so a
# table added in a later phase cannot quietly accumulate forever.
POLICIES: tuple[RetentionPolicy, ...] = (
    RetentionPolicy(
        table="trace_span",
        days=30,
        reason="debugging telemetry; nobody investigates a six-week-old latency spike",
    ),
    RetentionPolicy(
        table="conversation_message",
        days=90,
        reason="what a person asked and was told — sensitive, and stale quickly",
    ),
    RetentionPolicy(
        table="conversation",
        days=90,
        reason="matches its messages",
    ),
    RetentionPolicy(
        table="agent_run",
        days=90,
        reason="a run is a conversation that used tools",
    ),
    RetentionPolicy(
        table="ingest_job",
        days=30,
        reason="operational history; the documents it produced are the durable part",
    ),
    RetentionPolicy(
        table="connector_audit",
        days=365,
        reason="the compliance record — who ran what SQL. Asked for a year later",
    ),
    RetentionPolicy(
        table="approval_request",
        days=365,
        # Matches connector_audit, and for the same reason. This row is the
        # evidence that a named human approved a write which reached a
        # production system — the record somebody asks for when the question
        # is "who authorised this, and what exactly did they authorise". A
        # shorter period would delete the answer before the question arrives.
        #
        # Note the migration grants no DELETE on this table to app_rw:
        # retention runs as the owner. The application can propose, decide, and
        # execute; it cannot erase the trail.
        reason="evidence that a human approved a write; the record an auditor asks for",
    ),
    RetentionPolicy(
        table="training_record",
        days=365,
        # Same shape as approval_request: a human reviewed and activated the
        # profile that shapes what an integration is allowed to say. "Who
        # approved this integration's data profile, and what did it say" is a
        # compliance question, not operational noise.
        reason="evidence of a reviewed, activated integration data profile",
    ),
)

# Rows per table per run. Bounded so a first run against a large backlog cannot
# hold locks for minutes; the next run picks up where this one stopped.
BATCH_LIMIT = 10_000

# LangGraph's tables, which carry no tenant_id and are therefore reachable by
# neither RLS nor the tenant cascade. Deleting a tenant removes its agent_run
# rows and leaves the checkpoints behind forever.
#
# Measured on a development database after ten phases: 486 of 523 threads
# orphaned, 8.5 MB. Not a leak anyone would notice until it was large, which is
# how it survived being listed as a known gap since Phase 7.
#
# Cleaned by thread_id against agent_run — the table that DOES carry a tenant
# and DOES get cascaded — so a checkpoint outlives its run by exactly the
# agent_run retention period and no longer.
CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


@dataclass
class RetentionResult:
    deleted: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.deleted.values())

    def __str__(self) -> str:
        if not self.total:
            return "nothing to delete"
        parts = [f"{count} from {table}" for table, count in self.deleted.items() if count]
        return ", ".join(parts)


def apply_retention(
    session: Session,
    *,
    policies: tuple[RetentionPolicy, ...] = POLICIES,
    batch_limit: int = BATCH_LIMIT,
    dry_run: bool = False,
) -> RetentionResult:
    """Delete rows older than their policy allows.

    Runs as the OWNER, not as a tenant. Retention is administrative work across
    every tenant, and a tenant-scoped session would silently delete only the
    rows one tenant can see — which is worse than not running at all, because
    it looks like it worked.

    `dry_run` counts without deleting, so an operator can see what a first run
    would remove before it removes it.
    """
    deleted: dict[str, int] = {}

    for policy in policies:
        # ctid is Postgres's physical row identifier. Used here so the delete
        # can be capped by a subquery without needing every table to share a
        # primary-key column name.
        statement = (
            f"SELECT count(*) FROM {policy.table} "  # noqa: S608
            f"WHERE created_at < now() - make_interval(days => :days)"
            if dry_run
            else (
                f"DELETE FROM {policy.table} WHERE ctid IN ("  # noqa: S608
                f"  SELECT ctid FROM {policy.table}"
                f"  WHERE created_at < now() - make_interval(days => :days)"
                f"  LIMIT :limit"
                f")"
            )
        )

        result = session.execute(text(statement), {"days": policy.days, "limit": batch_limit})
        # cast: a DELETE returns CursorResult, which carries rowcount; the
        # declared return type of execute() is the broader Result.
        count = (
            int(result.scalar() or 0)
            if dry_run
            else int(cast("CursorResult[Any]", result).rowcount or 0)
        )
        deleted[policy.table] = count

        if count:
            logger.info(
                "%s %d row(s) from %s older than %d days",
                "would delete" if dry_run else "deleted",
                count,
                policy.table,
                policy.days,
            )

    deleted["checkpoints"] = _purge_orphaned_checkpoints(
        session, batch_limit=batch_limit, dry_run=dry_run
    )

    return RetentionResult(deleted=deleted)


def _purge_orphaned_checkpoints(session: Session, *, batch_limit: int, dry_run: bool) -> int:
    """Remove checkpoint rows whose agent_run is gone.

    Ordered child-first: checkpoint_writes and checkpoint_blobs reference a
    thread that checkpoints also holds, and removing the parent first would
    leave rows nothing can find.

    A thread with no agent_run is unreachable — resuming it requires the
    ownership record that agent_run provides, so a checkpoint without one can
    never be used again. Deleting it loses nothing.
    """
    total = 0

    for table in CHECKPOINT_TABLES:
        if dry_run:
            count = int(
                session.execute(
                    text(f"""
                        SELECT count(*) FROM {table} t
                        WHERE NOT EXISTS (
                            SELECT 1 FROM agent_run a WHERE a.thread_id = t.thread_id
                        )
                    """)  # noqa: S608 — table name from CHECKPOINT_TABLES
                ).scalar()
                or 0
            )
        else:
            result = session.execute(
                text(f"""
                    DELETE FROM {table} WHERE ctid IN (
                      SELECT t.ctid FROM {table} t
                      WHERE NOT EXISTS (
                          SELECT 1 FROM agent_run a WHERE a.thread_id = t.thread_id
                      )
                      LIMIT :limit
                    )
                """),  # noqa: S608 — table name from CHECKPOINT_TABLES
                {"limit": batch_limit},
            )
            count = int(cast("CursorResult[Any]", result).rowcount or 0)

        total += count
        if count:
            logger.info(
                "%s %d orphaned row(s) from %s",
                "would delete" if dry_run else "deleted",
                count,
                table,
            )

    return total
