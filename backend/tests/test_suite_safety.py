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
_ATTACK_STRING_FILES = frozenset({"test_sql_safety.py"})


def _test_files() -> list[Path]:
    return [p for p in TESTS_DIR.rglob("*.py") if p.name != Path(__file__).name]


def _files_that_must_not_mutate() -> list[Path]:
    return [p for p in _test_files() if p.name not in _ATTACK_STRING_FILES]


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
