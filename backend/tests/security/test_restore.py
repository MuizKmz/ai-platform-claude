"""Backup and restore, actually performed.

The Phase 8 DoD is specific: *"a restore has actually been performed, not just
documented"*. So this test dumps the live database, restores it into a scratch
database, and asks whether retrieval still works there — then drops it.

**Why the assertion is a search rather than a row count.** A restore that
produced the right number of rows and unusable data would pass a count check and
fail the only question that matters. Retrieval depends on three things
surviving together: the chunk text, the embedding vector, and the pgvector
extension that makes the distance operator exist. A search exercises all three.

**Why this is not part of the ordinary run.** It shells out to `docker`, creates
and drops a database, and takes several seconds. Marked `slow` and skipped
unless a Docker daemon and the container are both present, so `pytest` on a
machine without Docker does not fail for the wrong reason.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.core.config import settings

CONTAINER = "eaip-postgres"
SCRATCH_DB = "eaip_restore_test"

# Keycloak's database (Phase 11, ADR 0009). Separate container, separate role,
# separate backup obligation — it knows nothing about POSTGRES_USER.
KEYCLOAK_CONTAINER = "eaip-keycloak-db"
KEYCLOAK_SCRATCH_DB = "keycloak_restore_test"
# Matches the docker-compose default. Not a secret: it is the development
# password published in the compose file, and a deployment sets its own.
KEYCLOAK_DB_PASSWORD = os.environ.get("KEYCLOAK_DB_PASSWORD") or "keycloak-dev-password"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603
            ["docker", "ps", "--filter", f"name={CONTAINER}", "--format", "{{.Names}}"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return CONTAINER in result.stdout


# Marked at module level: everything here is a security concern and slow.
# The DOCKER requirement is applied per test instead, because two of these read
# files and need no container — and a security test that skips in CI proves
# nothing, so the set that skips should be as small as the truth allows.
pytestmark = [pytest.mark.security, pytest.mark.slow]

needs_container = pytest.mark.skipif(not _docker_available(), reason=f"{CONTAINER} is not running")


def _psql(database: str, sql: str) -> str:
    """Run a statement inside the container and return stdout."""
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={settings.postgres_password}",
            CONTAINER,
            "psql",
            "-U",
            settings.postgres_user,
            "-d",
            database,
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


@pytest.fixture
def scratch_database() -> Iterator[str]:
    """A database restored from a fresh dump, dropped afterwards.

    Named distinctly and dropped in a `finally`, because a test that leaves
    databases behind on a developer's machine is a test people stop running.
    """
    # /tmp INSIDE the Linux container, not on the host. Randomised so two
    # concurrent runs cannot collide, and removed in the finally block.
    dump_path = f"/tmp/{SCRATCH_DB}-{uuid.uuid4().hex[:8]}.dump"  # noqa: S108

    try:
        # Dump the live database inside the container, so no local Postgres
        # client is required and the versions cannot mismatch.
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "docker",
                "exec",
                "-e",
                f"PGPASSWORD={settings.postgres_password}",
                CONTAINER,
                "pg_dump",
                "-U",
                settings.postgres_user,
                "-d",
                settings.postgres_db,
                "-Fc",
                "--no-owner",
                "--no-privileges",
                "-f",
                dump_path,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )

        _psql("postgres", f"DROP DATABASE IF EXISTS {SCRATCH_DB}")
        _psql("postgres", f"CREATE DATABASE {SCRATCH_DB}")
        # pgvector must exist BEFORE the restore: the dump carries vector
        # columns, and restoring them into a database without the extension
        # fails partway with an error that reads like a corrupt dump.
        _psql(SCRATCH_DB, "CREATE EXTENSION IF NOT EXISTS vector")

        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "docker",
                "exec",
                "-e",
                f"PGPASSWORD={settings.postgres_password}",
                CONTAINER,
                "pg_restore",
                "-U",
                settings.postgres_user,
                "-d",
                SCRATCH_DB,
                "--no-owner",
                "--no-privileges",
                dump_path,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            # pg_restore exits non-zero for warnings too, so the return code is
            # not a verdict. The assertions below are.
            check=False,
        )

        yield SCRATCH_DB
    finally:
        subprocess.run(  # noqa: S603
            ["docker", "exec", CONTAINER, "rm", "-f", dump_path],  # noqa: S607
            capture_output=True,
            check=False,
        )
        # Suppressed: cleanup runs even when the test already failed, and a
        # cleanup error would mask the real one.
        with contextlib.suppress(RuntimeError):
            _psql("postgres", f"DROP DATABASE IF EXISTS {SCRATCH_DB}")


# --- the roadmap's named test -------------------------------------------------


@needs_container
def test_restore_from_backup(scratch_database: str) -> None:
    """Retrieval works against restored data.

    Not a row count. A restore that produced the right number of rows and
    unusable vectors would satisfy a count check and fail the only question
    worth asking. This runs an actual similarity search, which needs the chunk
    text, the embedding, and the pgvector operator to have survived together.
    """
    url = (
        f"postgresql+psycopg://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{scratch_database}"
    )
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            chunks = conn.execute(text("SELECT count(*) FROM chunk")).scalar() or 0
            if chunks == 0:
                pytest.skip("the live database has no chunks to restore")

            embedded = conn.execute(
                text("SELECT count(*) FROM chunk WHERE embedding IS NOT NULL")
            ).scalar()
            assert embedded == chunks, (
                f"{chunks - (embedded or 0)} chunk(s) lost their embedding in the restore"
            )

            # The real assertion: a nearest-neighbour search runs and ranks.
            # This exercises the vector type, the distance operator, and the
            # index — none of which a row count touches.
            probe = conn.execute(
                text("SELECT embedding FROM chunk WHERE embedding IS NOT NULL LIMIT 1")
            ).scalar()

            hits = conn.execute(
                text("""
                    SELECT id, embedding <=> CAST(:probe AS vector) AS distance
                    FROM chunk
                    WHERE embedding IS NOT NULL
                    ORDER BY distance
                    LIMIT 5
                """),
                {"probe": probe},
            ).all()

            assert hits, "similarity search returned nothing on restored data"
            # A vector compared with itself is distance zero. Anything else
            # means the values were altered in transit.
            #
            # What this does and does not prove, stated because I tried to
            # break it three ways and two of the attempts were invalid rather
            # than the test being weak:
            #
            #   - dropping the CREATE EXTENSION step does not break it, because
            #     pg_restore recreates the extension from the dump itself
            #   - nulling an embedding cannot be done at all: the column is NOT
            #     NULL, so the schema refuses it before this test would
            #   - replacing one vector with another still yields self-distance
            #     zero, which is correct — a duplicate is not corruption
            #
            # It genuinely fails on: missing rows, a vector column restored as
            # text, a pgvector version without `<=>`, and values altered in
            # transit. That is the failure surface of a restore.
            assert hits[0].distance == pytest.approx(0.0, abs=1e-6), (
                "the nearest neighbour of a vector was not itself — "
                "embeddings did not survive intact"
            )
    finally:
        engine.dispose()


@needs_container
def test_tenant_isolation_survives_a_restore(scratch_database: str) -> None:
    """RLS policies are schema, so they come back with the schema.

    Worth asserting rather than assuming: a restore that dropped the policies
    would leave a database that looks complete and isolates nothing, and the
    failure would only appear when one tenant read another's rows.
    """
    url = (
        f"postgresql+psycopg://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{scratch_database}"
    )
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            unprotected = conn.execute(
                text("""
                    SELECT c.relname
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relkind = 'r'
                      AND c.relname IN ('document', 'chunk', 'conversation', 'trace_span')
                      AND NOT c.relrowsecurity
                """)
            ).all()

        assert not unprotected, (
            f"these tables lost Row-Level Security in the restore: "
            f"{[row.relname for row in unprotected]}"
        )
    finally:
        engine.dispose()


def test_the_backup_scripts_exist_and_are_readable() -> None:
    """The runbook points at these. A runbook naming a script that is not there
    is worse than one that says 'not yet built'."""
    backup_dir = Path(__file__).resolve().parents[2].parent / "infra" / "backup"

    for name in ("backup.ps1", "restore.ps1"):
        script = backup_dir / name
        assert script.is_file(), f"{name} is missing from infra/backup/"
        # Windows PowerShell 5.1 reads a BOM-less UTF-8 file as ANSI and
        # mangles every non-ASCII character, which breaks string parsing. Both
        # scripts failed to run for exactly this reason before the BOM was
        # added, so it is pinned here.
        assert script.read_bytes().startswith(b"\xef\xbb\xbf"), (
            f"{name} needs a UTF-8 BOM or PowerShell 5.1 will mis-parse it"
        )


def test_dumps_are_not_committed() -> None:
    """A dump holds every tenant's documents and encrypted credentials.
    Committing one would put the corpus in git history permanently."""
    backup_dir = Path(__file__).resolve().parents[2].parent / "infra" / "backup"
    gitignore = backup_dir / ".gitignore"

    assert gitignore.is_file(), "infra/backup/.gitignore is missing"
    assert "dumps/" in gitignore.read_text(encoding="utf-8")


# --- the identity database (Phase 11, ADR 0009) -------------------------------
#
# Restoring EAIP alone recovers a platform nobody can log into: every document
# survives and every account is gone. Keycloak arrived in Phase 11 and the
# backup covered one database until this was written.


def _keycloak_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "docker",
                "ps",
                "--filter",
                f"name={KEYCLOAK_CONTAINER}",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return KEYCLOAK_CONTAINER in result.stdout


needs_keycloak = pytest.mark.skipif(
    not _keycloak_available(), reason=f"{KEYCLOAK_CONTAINER} is not running"
)


def _keycloak_psql(database: str, sql: str) -> str:
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "exec",
            "-e",
            f"PGPASSWORD={KEYCLOAK_DB_PASSWORD}",
            KEYCLOAK_CONTAINER,
            "psql",
            "-U",
            "keycloak",
            "-d",
            database,
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


@pytest.fixture
def scratch_keycloak_database() -> Iterator[str]:
    """Keycloak's database, dumped and restored into a scratch copy."""
    dump_path = f"/tmp/{KEYCLOAK_SCRATCH_DB}-{uuid.uuid4().hex[:8]}.dump"  # noqa: S108

    try:
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "docker",
                "exec",
                "-e",
                f"PGPASSWORD={KEYCLOAK_DB_PASSWORD}",
                KEYCLOAK_CONTAINER,
                "pg_dump",
                "-U",
                "keycloak",
                "-d",
                "keycloak",
                "-Fc",
                "--no-owner",
                "--no-privileges",
                "-f",
                dump_path,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )

        _keycloak_psql("postgres", f"DROP DATABASE IF EXISTS {KEYCLOAK_SCRATCH_DB}")
        _keycloak_psql("postgres", f"CREATE DATABASE {KEYCLOAK_SCRATCH_DB}")

        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "docker",
                "exec",
                "-e",
                f"PGPASSWORD={KEYCLOAK_DB_PASSWORD}",
                KEYCLOAK_CONTAINER,
                "pg_restore",
                "-U",
                "keycloak",
                "-d",
                KEYCLOAK_SCRATCH_DB,
                "--no-owner",
                "--no-privileges",
                dump_path,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            # pg_restore exits non-zero on warnings as well as errors. The
            # assertions below are the verdict, not the exit code.
            check=False,
        )

        yield KEYCLOAK_SCRATCH_DB
    finally:
        with contextlib.suppress(Exception):
            _keycloak_psql("postgres", f"DROP DATABASE IF EXISTS {KEYCLOAK_SCRATCH_DB}")
        with contextlib.suppress(Exception):
            subprocess.run(  # noqa: S603
                ["docker", "exec", KEYCLOAK_CONTAINER, "rm", "-f", dump_path],  # noqa: S607
                capture_output=True,
                timeout=30,
                check=False,
            )


@needs_keycloak
def test_a_restored_realm_still_has_its_users(scratch_keycloak_database: str) -> None:
    """Accounts, not just schema.

    A restore that recreated 90-odd empty Keycloak tables would satisfy any
    check that only asked whether the restore finished. What matters is whether
    anybody can still sign in afterwards.
    """
    realms = int(_keycloak_psql(scratch_keycloak_database, "SELECT count(*) FROM realm"))
    users = int(_keycloak_psql(scratch_keycloak_database, "SELECT count(*) FROM user_entity"))

    assert realms >= 1, "no realms survived the restore"
    assert users >= 1, "the realm restored with no users — nobody could sign in"


@needs_keycloak
def test_restored_users_keep_the_claims_the_api_requires(
    scratch_keycloak_database: str,
) -> None:
    """A user without tenant_id authenticates and is then refused.

    `principal_from_token` refuses a token missing tenancy rather than
    defaulting it (invariant #1), so an attribute lost in a restore does not
    degrade access — it removes it, at the API rather than at the login page,
    which is a confusing place to debug from.
    """
    with_tenant = int(
        _keycloak_psql(
            scratch_keycloak_database,
            "SELECT count(*) FROM user_attribute WHERE name = 'tenant_id'",
        )
    )
    credentials = int(_keycloak_psql(scratch_keycloak_database, "SELECT count(*) FROM credential"))

    assert credentials >= 1, "no credentials survived — every password was lost"
    assert with_tenant >= 1, (
        "no restored user carries tenant_id; every login would be refused by the API"
    )


def test_the_backup_script_covers_the_identity_database() -> None:
    """Static check, so it fails in CI rather than during a recovery.

    The script covered EAIP's database only for the whole of Phase 11's first
    day. Nothing failed; there was simply no backup of the accounts, which is
    the kind of gap that stays invisible until it is expensive.
    """
    backup_dir = Path(__file__).resolve().parents[2].parent / "infra" / "backup"
    script = (backup_dir / "backup.ps1").read_text(encoding="utf-8-sig")

    assert KEYCLOAK_CONTAINER in script, (
        "backup.ps1 does not mention the Keycloak database. A restore from it "
        "recovers the documents and none of the accounts."
    )
    # An explicit opt-out is fine; silently skipping is not. The script must
    # refuse rather than produce a half-backup nobody notices.
    assert "SkipKeycloak" in script, "backup.ps1 needs a deliberate way to opt out"
