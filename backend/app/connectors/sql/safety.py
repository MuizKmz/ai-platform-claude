"""SQL safety: the four application-side layers.

The fifth and most important layer is not here, because it is not code: it is a
Postgres role granted `SELECT` on curated views and nothing else. Everything in
this file is a fast, informative filter in front of that role — not a substitute
for it.

That ordering matters. An AST validator will eventually be defeated by a
construction nobody anticipated; a `GRANT` will not. So this module aims to
reject bad SQL *clearly and early*, while assuming it will one day miss something.

**Allowlist, never blocklist.** A blocklist of forbidden keywords is a losing
game: comment injection, unicode homoglyphs, dialect quirks, and functions nobody
on the team has heard of all route around it. An allowlist of permitted AST node
types fails closed — anything unrecognised is refused, including constructs that
did not exist when this was written.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

DIALECT = "postgres"

# Hard cap on returned rows. A query with no LIMIT gets one; a larger LIMIT is
# lowered to this. Unbounded results are both a memory problem and a bulk-export
# route out of a system that otherwise answers one question at a time.
MAX_ROWS = 1000

# Every AST node type a read-only analytical query legitimately needs. Anything
# absent is refused, which is what makes this fail closed.
_ALLOWED_NODES: frozenset[type[exp.Expression]] = frozenset(
    {
        # Query shape
        exp.Select,
        exp.From,
        exp.Where,
        exp.Group,
        exp.Having,
        exp.Order,
        exp.Limit,
        exp.Offset,
        exp.Distinct,
        exp.Subquery,
        exp.With,
        exp.CTE,
        exp.Union,
        exp.Intersect,
        exp.Except,
        exp.Ordered,
        # Joins
        exp.Join,
        exp.Lateral,
        # References
        exp.Table,
        exp.Column,
        exp.Identifier,
        exp.Alias,
        exp.TableAlias,
        exp.Star,
        # Literals and types
        exp.Literal,
        exp.Boolean,
        exp.Null,
        exp.DataType,
        exp.Cast,
        exp.TryCast,
        exp.Interval,
        exp.Array,
        exp.Tuple,
        exp.Paren,
        # Comparison and logic
        exp.EQ,
        exp.NEQ,
        exp.GT,
        exp.GTE,
        exp.LT,
        exp.LTE,
        exp.And,
        exp.Or,
        exp.Not,
        exp.In,
        exp.Between,
        exp.Like,
        exp.ILike,
        exp.Is,
        exp.Exists,
        exp.Any,
        exp.All,
        # Arithmetic
        exp.Add,
        exp.Sub,
        exp.Mul,
        exp.Div,
        exp.Mod,
        exp.Neg,
        exp.Pow,
        # Conditionals
        exp.Case,
        exp.If,
        exp.Coalesce,
        exp.Nullif,
        # Aggregates and windows
        exp.Count,
        exp.Sum,
        exp.Avg,
        exp.Min,
        exp.Max,
        exp.Window,
        exp.WindowSpec,
        exp.RowNumber,
        exp.Rank,
        exp.DenseRank,
        exp.Lag,
        exp.Lead,
        exp.PartitionByTruncate,
        # Common scalar functions
        exp.Anonymous,
        exp.Func,
        exp.Concat,
        exp.Substring,
        exp.Length,
        exp.Lower,
        exp.Upper,
        exp.Trim,
        exp.Round,
        exp.Abs,
        exp.Extract,
        exp.DateTrunc,
        exp.DateAdd,
        exp.DateDiff,
        exp.CurrentDate,
        exp.CurrentTimestamp,
    }
)

# Functions that read files, sleep, open connections, or reveal the environment.
# This IS a blocklist, and it is a backstop rather than a defence: the read-only
# role cannot execute most of these anyway, and exp.Anonymous is permitted above
# so that ordinary unknown functions are not rejected wholesale.
_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "dblink",
        "dblink_connect",
        "dblink_exec",
        "lo_import",
        "lo_export",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "current_setting",
        "set_config",
        "query_to_xml",
        "xmlexec",
        "copy_from",
        "copy_to",
    }
)


class UnsafeSQLError(Exception):
    """The statement was refused. Safe to show a caller: it explains what was
    rejected, which is information they supplied."""


@dataclass(frozen=True)
class SafeSQL:
    """A statement that passed every application-side check."""

    sql: str
    limit: int
    # True if a LIMIT was added or lowered — surfaced so a user is not silently
    # shown a truncated answer to a question about totals.
    limit_applied: bool


def validate(sql: str, *, max_rows: int = MAX_ROWS, schema: str = "curated") -> SafeSQL:
    """Parse, validate, and normalise a statement, or raise UnsafeSQLError."""
    statements = _parse(sql)

    # Layer: exactly one statement. "SELECT 1; DROP TABLE t" is two, and a driver
    # that supports multiple statements would happily run both.
    if len(statements) != 1:
        raise UnsafeSQLError(
            f"Expected exactly one statement, found {len(statements)}. "
            "Stacked statements are not permitted."
        )

    statement = statements[0]

    # Layer: the top level must be a SELECT (or a WITH ending in one).
    if not isinstance(statement, exp.Select | exp.Union | exp.Intersect | exp.Except):
        raise UnsafeSQLError(
            f"Only SELECT statements are permitted; found {type(statement).__name__.upper()}."
        )

    _assert_no_writes(statement)
    _assert_allowed_nodes(statement)
    _assert_no_forbidden_functions(statement)
    _assert_only_curated_tables(statement, schema)

    # Narrowed above: the top level is Select/Union/Intersect/Except, all of
    # which are exp.Query and therefore carry .limit().
    limited, applied = _apply_limit(statement, max_rows)
    return SafeSQL(sql=limited, limit=max_rows, limit_applied=applied)


def _parse(sql: str) -> list[exp.Expression]:
    try:
        parsed = sqlglot.parse(sql, dialect=DIALECT)
    except Exception as exc:  # noqa: BLE001 — sqlglot raises several types
        raise UnsafeSQLError("The statement could not be parsed.") from exc

    statements = [s for s in parsed if s is not None]
    if not statements:
        raise UnsafeSQLError("No statement was found.")
    return statements


def _assert_no_writes(statement: exp.Expression) -> None:
    """Refuse any write node ANYWHERE in the tree.

    The important case is a CTE: `WITH x AS (DELETE FROM t RETURNING *) SELECT *
    FROM x` has a SELECT at the top level and deletes rows. Checking only the
    root node would pass it.
    """
    write_nodes = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Alter,
        exp.TruncateTable,
        exp.Grant,
        exp.Copy,
        exp.Merge,
        exp.Command,
    )
    for node in statement.walk():
        if isinstance(node, write_nodes):
            raise UnsafeSQLError(
                f"{type(node).__name__.upper()} is not permitted. This connector is read-only."
            )


def _assert_allowed_nodes(statement: exp.Expression) -> None:
    """Every node must be on the allowlist. Unrecognised constructs are refused."""
    for node in statement.walk():
        if not isinstance(node, tuple(_ALLOWED_NODES)):
            raise UnsafeSQLError(
                f"{type(node).__name__} is not an allowed SQL construct. "
                "Only read-only analytical queries are permitted."
            )


def _assert_no_forbidden_functions(statement: exp.Expression) -> None:
    """Reject file, sleep, and connection functions by name.

    Necessary because exp.Anonymous covers any function sqlglot does not model,
    which is what keeps ordinary functions working — and would otherwise let
    pg_read_file through the node allowlist.
    """
    for node in statement.walk():
        name: str | None = None
        if isinstance(node, exp.Anonymous):
            name = str(node.this)
        elif isinstance(node, exp.Func):
            # sqlglot ships no annotation for sql_name(); it returns the SQL
            # function name as a str.
            name = str(node.sql_name())  # type: ignore[no-untyped-call]

        if name and name.lower() in _FORBIDDEN_FUNCTIONS:
            raise UnsafeSQLError(f"Function {name}() is not permitted.")


def _apply_limit(statement: exp.Query, max_rows: int) -> tuple[str, bool]:
    """Ensure a LIMIT no larger than max_rows.

    Injected rather than merely checked: a missing LIMIT is the difference
    between answering a question and exporting a table.
    """
    existing = statement.args.get("limit")
    applied = False

    if existing is None:
        statement = statement.limit(max_rows)
        applied = True
    else:
        try:
            current = int(existing.expression.this)
        except (AttributeError, TypeError, ValueError):
            # A non-literal LIMIT (a parameter or expression) cannot be compared,
            # so it is replaced with the cap rather than trusted.
            statement.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
            return statement.sql(dialect=DIALECT), True
        if current > max_rows:
            statement.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
            applied = True

    return statement.sql(dialect=DIALECT), applied


def _assert_only_curated_tables(statement: exp.Expression, schema: str) -> None:
    """Every table referenced must live in the curated schema.

    Added after the red-team corpus found the gap: the validator checked node
    types and functions but never table NAMES, so `SELECT * FROM pg_user` passed
    every layer and reached the database — where Postgres grants that catalogue
    to PUBLIC and happily returned it.

    No credential leaked (passwords read as `********`), but role names, which
    role is superuser, every database name, and 72 table names across the
    cluster did. That is reconnaissance, and it was reachable by asking the
    agent a question.

    The read-only role remains the backstop and blocked the worse targets —
    `customers`, `pg_shadow`. This layer means a generated query never gets to
    ask. Defence in depth: the grant is the control, this is the thing that
    stops the control being the only one.

    CTE aliases are exempt: `WITH recent AS (SELECT ... FROM curated.v_orders)
    SELECT * FROM recent` references `recent`, which is not a table.
    """
    cte_names = {
        cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE) if cte.alias_or_name
    }

    for table in statement.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name or name in cte_names:
            continue

        table_schema = (table.db or "").lower()
        if table_schema != schema.lower():
            raise UnsafeSQLError(
                f"Only tables in the {schema!r} schema may be queried. "
                f"Qualify the table as {schema}.<view_name>."
            )
