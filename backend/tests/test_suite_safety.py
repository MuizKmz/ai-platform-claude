"""The test suite must not destroy the developer's data.

This exists because it already happened: an unscoped DELETE FROM tenant in a
fixture wiped real local tenants every time the suite ran. The failure was silent
— tests passed, data vanished — so it needs a test of its own rather than a note
in a review checklist.
"""

import re
from pathlib import Path

TESTS_DIR = Path(__file__).parent

# A DELETE with no WHERE clause on the same line. Deliberately simple: the point
# is to catch the obvious, dangerous shape, not to parse SQL.
_UNSCOPED_DELETE = re.compile(r"DELETE\s+FROM\s+(\w+)\s*(?:\"|'|\)|$)", re.IGNORECASE)

_TENANT_TABLES = {"tenant", "document", "chunk", "app_user", "trace_span"}

# Slugs a developer plausibly uses for real local work. A fixture claiming one
# collides on the unique index and fails the suite for a reason that looks like a
# code bug rather than a naming clash.
_RESERVED_SLUGS = {"acme", "globex", "test", "demo", "default"}

_SLUG_LITERAL = re.compile(r"INSERT INTO tenant.*?'([a-z0-9-]+)'", re.IGNORECASE | re.DOTALL)


# Files whose whole purpose is to assert that dangerous SQL is REFUSED. They
# necessarily contain the statements they are proving cannot run, and every one
# sits inside a pytest.raises. Exempting them by name keeps the guard meaningful
# elsewhere; a blanket "ignore anything in a raises block" would be easy to
# accidentally satisfy.
_ATTACK_STRING_FILES = frozenset({"test_sql_safety.py", "test_redteam_corpus.py"})

# Files that genuinely EXECUTE a DROP, and are allowed to. A different category
# from the above, and a narrower one: these do the dangerous thing rather than
# proving it is refused.
#
# test_restore.py drops a scratch database named `eaip_restore_test`, which it
# also creates. That is the only way to prove a restore works — the Phase 8 DoD
# asks for a restore "actually performed, not just documented", and you cannot
# perform one without somewhere to perform it into.
#
# The exemption is kept honest by test_the_drop_exemption_is_narrow below: the
# guard would be worthless if a file could opt out of it by being added here.
_MAY_DROP_A_SCRATCH_DATABASE = frozenset({"test_restore.py"})

# Databases a test is permitted to drop. Anything else is a bug, and naming
# them makes "DROP DATABASE eaip" impossible to add by accident.
_DROPPABLE_DATABASES = frozenset({"eaip_restore_test"})


def _test_files() -> list[Path]:
    return [p for p in TESTS_DIR.rglob("*.py") if p.name != Path(__file__).name]


def _files_that_must_not_mutate() -> list[Path]:
    exempt = _ATTACK_STRING_FILES | _MAY_DROP_A_SCRATCH_DATABASE
    return [p for p in _test_files() if p.name not in exempt]


def test_no_test_issues_an_unscoped_delete() -> None:
    """Every DELETE against a tenant table must carry a WHERE clause.

    Scoping to the fixture's own tenant ids is what keeps `uv run pytest` safe to
    run against a database that also holds real work.
    """
    offenders: list[str] = []

    for path in _files_that_must_not_mutate():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = _UNSCOPED_DELETE.search(line)
            if match and match.group(1).lower() in _TENANT_TABLES:
                offenders.append(f"{path.relative_to(TESTS_DIR)}:{lineno}: {line.strip()}")

    joined = "\n".join(offenders)
    assert not offenders, f"unscoped DELETE in tests would destroy real data:\n{joined}"


def test_fixture_slugs_do_not_collide_with_real_ones() -> None:
    """Test tenants must be namespaced so they cannot clash with local data."""
    offenders: list[str] = []

    for path in _test_files():
        for slug in _SLUG_LITERAL.findall(path.read_text(encoding="utf-8")):
            if slug in _RESERVED_SLUGS:
                offenders.append(f"{path.relative_to(TESTS_DIR)}: fixture uses slug {slug!r}")

    joined = "\n".join(offenders)
    assert not offenders, f"fixture slugs collide with likely real tenants:\n{joined}"


def test_no_test_truncates_or_drops() -> None:
    """TRUNCATE and DROP have no safe scoped form here; they must not appear.

    Exempt: files whose purpose is proving such statements are REFUSED. They
    contain the statements as attack strings inside pytest.raises, never as work
    the suite performs.
    """
    offenders: list[str] = []

    for path in _files_that_must_not_mutate():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"\b(TRUNCATE|DROP\s+TABLE|DROP\s+DATABASE)\b", line, re.IGNORECASE):
                offenders.append(f"{path.relative_to(TESTS_DIR)}:{lineno}: {line.strip()}")

    joined = "\n".join(offenders)
    assert not offenders, f"destructive DDL in tests:\n{joined}"


def test_the_drop_exemption_is_narrow() -> None:
    """A file allowed to DROP may only drop the scratch databases named here.

    The exemption above would be worthless if adding a filename to it were a
    way to opt out of the guard entirely. This re-imposes the check on exactly
    the files that were let through, permitting only the named scratch
    databases — so `DROP DATABASE eaip` in test_restore.py fails here even
    though that file is exempt from the broad rule.
    """
    droppable = "|".join(re.escape(name) for name in _DROPPABLE_DATABASES)
    permitted = re.compile(
        r"DROP\s+DATABASE\s+(IF\s+EXISTS\s+)?(\{SCRATCH_DB\}|" + droppable + r")",
        re.IGNORECASE,
    )
    offenders: list[str] = []

    for path in _test_files():
        if path.name not in _MAY_DROP_A_SCRATCH_DATABASE:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"\bDROP\s+DATABASE\b", line, re.IGNORECASE) and not permitted.search(
                line
            ):
                offenders.append(f"{path.relative_to(TESTS_DIR)}:{lineno}: {line.strip()}")

    joined = "\n".join(offenders)
    assert not offenders, (
        f"a DROP DATABASE outside the permitted scratch names:\n{joined}\n"
        f"Permitted: {sorted(_DROPPABLE_DATABASES)}"
    )


def test_the_scratch_database_name_cannot_be_a_real_one() -> None:
    """The scratch name must not collide with anything real."""
    from app.core.config import settings

    assert settings.postgres_db not in _DROPPABLE_DATABASES, (
        f"the live database {settings.postgres_db!r} is in the droppable list"
    )
    for name in _DROPPABLE_DATABASES:
        assert "test" in name, f"scratch database {name!r} is not obviously a test database"
