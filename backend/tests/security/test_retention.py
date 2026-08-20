"""Data retention.

The test that matters most is `test_every_time_series_table_has_a_policy`. The
others prove the deletion works; that one proves nobody forgot a table.

Before this module existed, nothing in this system deleted anything — every
trace, message, and audit row accumulated forever. The failure mode was not a
bug anyone would hit, which is exactly why it survived seven phases: a table
that grows without bound looks fine until it does not, and data held without a
reason is pure downside.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.retention import POLICIES, RetentionPolicy, apply_retention


def _database_available() -> bool:
    try:
        engine = create_engine(settings.database_url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


pytestmark = [
    pytest.mark.security,
    pytest.mark.skipif(not _database_available(), reason="no database reachable"),
]

TENANT = uuid.UUID("4e770000-0000-0000-0000-00000000004e")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    eng = create_engine(settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def tenant(engine: Engine) -> Iterator[None]:
    def _wipe() -> None:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM trace_span WHERE tenant_id = :t"), {"t": TENANT})
            conn.execute(text("DELETE FROM tenant WHERE id = :t"), {"t": TENANT})

    _wipe()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenant (id, slug, name) VALUES (:t, 'ret-test', 'Retention')"),
            {"t": TENANT},
        )
    yield
    _wipe()


def _plant_span(conn: object, *, days_old: int) -> uuid.UUID:
    """A trace span with a backdated created_at."""
    span_id = uuid.uuid4()
    conn.execute(  # type: ignore[attr-defined]
        text("""
            INSERT INTO trace_span
              (id, trace_id, span_id, name, tenant_id, duration_ms, status,
               attributes, created_at)
            VALUES
              (:id, :trace, :span, 'test', :t, 1.0, 'ok', '{}',
               now() - make_interval(days => :days))
        """),
        {
            "id": span_id,
            "trace": uuid.uuid4().hex,
            "span": uuid.uuid4().hex[:16],
            "t": TENANT,
            "days": days_old,
        },
    )
    return span_id


# --- the guard ----------------------------------------------------------------


def test_every_time_series_table_has_a_policy(engine: Engine) -> None:
    """A table that accumulates rows must say how long it keeps them.

    Discovered by scanning for tables with both `tenant_id` and `created_at` —
    the shape of something that grows without bound. A table added in a later
    phase fails this test until someone decides its retention, which is the
    point: the decision should be deliberate rather than defaulted to forever.

    Tables legitimately exempt are listed with a reason. `document` and `chunk`
    hold the corpus itself, which is deleted by lifecycle rules rather than by
    age; `connector`, `app_user`, and `tenant` are configuration, not history.
    """
    exempt = {
        "document": "the corpus; deleted by lifecycle, not by age",
        "chunk": "belongs to its document",
        "connector": "configuration, not history",
        "app_user": "configuration, not history",
        "tenant": "configuration, not history",
    }

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT c.table_name
                FROM information_schema.columns c
                JOIN information_schema.columns c2
                  ON c2.table_name = c.table_name AND c2.column_name = 'created_at'
                WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
                GROUP BY c.table_name
            """)
        ).all()

    accumulating = {row.table_name for row in rows} - set(exempt)
    covered = {policy.table for policy in POLICIES}
    missing = sorted(accumulating - covered)

    assert not missing, (
        f"these tables grow forever with no retention policy: {missing}. "
        "Add one to POLICIES, or add it to `exempt` here with a reason."
    )


def test_the_audit_log_is_kept_longest() -> None:
    """The compliance record outlives the conversations it describes.

    Asserted because it is the one period most likely to be "tidied" to match
    the others by someone who has not thought about who asks for it, and when.
    """
    by_table = {policy.table: policy.days for policy in POLICIES}
    assert by_table["connector_audit"] > by_table["conversation_message"]
    assert by_table["connector_audit"] >= 365


def test_traces_are_kept_shortest() -> None:
    """Highest volume, least sensitive, shortest useful life."""
    by_table = {policy.table: policy.days for policy in POLICIES}
    assert by_table["trace_span"] <= min(
        days for table, days in by_table.items() if table != "trace_span"
    )


# --- deletion behaviour -------------------------------------------------------


def test_rows_past_their_policy_are_deleted(engine: Engine) -> None:
    with engine.begin() as conn:
        old = _plant_span(conn, days_old=45)
        recent = _plant_span(conn, days_old=5)

    with Session(engine) as session:
        result = apply_retention(session)
        session.commit()

    assert result.deleted["trace_span"] >= 1

    with engine.connect() as conn:
        survivors = {
            row.id
            for row in conn.execute(
                text("SELECT id FROM trace_span WHERE tenant_id = :t"), {"t": TENANT}
            ).all()
        }

    assert old not in survivors, "a span past its retention period survived"
    assert recent in survivors, "a span inside its retention period was deleted"


def test_a_dry_run_deletes_nothing(engine: Engine) -> None:
    """An operator must be able to see what a first run would remove before it
    removes it — a first run against years of backlog is not something to
    discover the shape of afterwards."""
    with engine.begin() as conn:
        old = _plant_span(conn, days_old=45)

    with Session(engine) as session:
        result = apply_retention(session, dry_run=True)
        session.commit()

    assert result.deleted["trace_span"] >= 1, "the dry run counted nothing"

    with engine.connect() as conn:
        still_there = conn.execute(
            text("SELECT count(*) FROM trace_span WHERE id = :id"), {"id": old}
        ).scalar()
    assert still_there == 1, "a dry run deleted a row"


def test_running_twice_is_harmless(engine: Engine) -> None:
    """Idempotent: the second run finds nothing left to do."""
    with engine.begin() as conn:
        _plant_span(conn, days_old=45)

    with Session(engine) as session:
        first = apply_retention(session)
        session.commit()
        second = apply_retention(session)
        session.commit()

    assert first.deleted["trace_span"] >= 1
    assert second.deleted["trace_span"] == 0


def test_the_batch_limit_is_respected(engine: Engine) -> None:
    """A first run against a large backlog must not try to delete it all at
    once — the next run picks up where this one stopped."""
    with engine.begin() as conn:
        for _ in range(5):
            _plant_span(conn, days_old=45)

    with Session(engine) as session:
        result = apply_retention(session, batch_limit=2)
        session.commit()

    assert result.deleted["trace_span"] == 2, "the batch limit was ignored"

    with engine.connect() as conn:
        remaining = conn.execute(
            text("SELECT count(*) FROM trace_span WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar()
    assert remaining == 3


def test_a_custom_policy_is_honoured(engine: Engine) -> None:
    """The policy is data, so a deployment with different obligations can pass
    its own without editing this module."""
    with engine.begin() as conn:
        recent = _plant_span(conn, days_old=5)

    with Session(engine) as session:
        # One day, not thirty: the "recent" span is now past its period.
        result = apply_retention(
            session,
            policies=(RetentionPolicy(table="trace_span", days=1, reason="test"),),
        )
        session.commit()

    assert result.deleted["trace_span"] >= 1

    with engine.connect() as conn:
        gone = conn.execute(
            text("SELECT count(*) FROM trace_span WHERE id = :id"), {"id": recent}
        ).scalar()
    assert gone == 0


# --- LangGraph checkpoints ----------------------------------------------------


def test_orphaned_checkpoints_are_purged(engine: Engine) -> None:
    """Checkpoint rows whose agent_run is gone are removed.

    LangGraph's tables carry no tenant_id, so RLS does not reach them and
    `DELETE FROM tenant` does not cascade to them. Deleting a tenant removed its
    agent_run rows and left the checkpoints behind forever.

    Measured on a development database after ten phases: 486 of 523 threads
    orphaned, 8.5 MB. Listed as a known gap since Phase 7 and never closed,
    because nothing surfaces a leak until it is large.

    A thread with no agent_run is unreachable — resuming needs the ownership
    record agent_run provides — so deleting it loses nothing.
    """
    orphan_thread = f"orphan-{uuid.uuid4().hex}"
    kept_thread = f"kept-{uuid.uuid4().hex}"

    with engine.begin() as conn:
        # An orphan: a checkpoint with no agent_run.
        conn.execute(
            text("""
                INSERT INTO checkpoints
                  (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata)
                VALUES (:t, '', :c, '{}', '{}')
            """),
            {"t": orphan_thread, "c": uuid.uuid4().hex},
        )
        # And one whose run still exists.
        conn.execute(
            text("""
                INSERT INTO agent_run (id, tenant_id, user_id, thread_id, question)
                VALUES (:id, :t, :u, :thread, 'q')
            """),
            {"id": uuid.uuid4(), "t": TENANT, "u": uuid.uuid4(), "thread": kept_thread},
        )
        conn.execute(
            text("""
                INSERT INTO checkpoints
                  (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata)
                VALUES (:t, '', :c, '{}', '{}')
            """),
            {"t": kept_thread, "c": uuid.uuid4().hex},
        )

    with Session(engine) as session:
        apply_retention(session)
        session.commit()

    with engine.connect() as conn:
        orphan_left = conn.execute(
            text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"),
            {"t": orphan_thread},
        ).scalar()
        kept_left = conn.execute(
            text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"),
            {"t": kept_thread},
        ).scalar()

    assert orphan_left == 0, "an orphaned checkpoint survived"
    assert kept_left == 1, "a checkpoint with a live agent_run was deleted"

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM checkpoints WHERE thread_id = :t"), {"t": kept_thread})


def test_a_dry_run_counts_orphaned_checkpoints_without_deleting(
    engine: Engine,
) -> None:
    """An operator sees the scale of a first purge before running it."""
    thread = f"orphan-{uuid.uuid4().hex}"
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO checkpoints
                  (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata)
                VALUES (:t, '', :c, '{}', '{}')
            """),
            {"t": thread, "c": uuid.uuid4().hex},
        )

    with Session(engine) as session:
        result = apply_retention(session, dry_run=True)
        session.commit()

    assert result.deleted["checkpoints"] >= 1

    with engine.connect() as conn:
        still_there = conn.execute(
            text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"), {"t": thread}
        ).scalar()
    assert still_there == 1, "a dry run deleted a checkpoint"

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM checkpoints WHERE thread_id = :t"), {"t": thread})
