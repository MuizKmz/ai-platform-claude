# ADR 0008 — PyMySQL for the MySQL connector

**Status:** Accepted
**Date:** 2026-08-21
**Phase:** 10.5 — between MCP and production deployment

## Context

Every SQL safety layer in this platform was built and tested against
**PostgreSQL**. The security argument is *"the database refuses writes"*, and
that has been proven for one database.

The systems this platform exists to serve — an ERP, a WMS, an MES, and an IoT
platform — all run **MySQL**. So the claim currently holds for a database
nobody here uses.

Two things need a MySQL driver:

1. **The connector itself**, so a generated query can reach a MySQL source.
2. **The safety tests**, which must run against a real MySQL rather than a
   mock. `test_write_rejected_at_db_level_too` bypasses the AST validator
   entirely and hands a write to the role; a mock cannot answer the question it
   asks.

## Decision

**PyMySQL**, as a runtime dependency, used through SQLAlchemy's
`mysql+pymysql://` dialect.

## Alternatives considered

| Option | Why not |
|---|---|
| **mysqlclient** | Faster — it wraps the C client. But it needs `libmysqlclient` headers at build time, which means the Docker image grows a compiler and the lockfile stops being portable between a Windows dev machine and a Linux runner. We are not query-throughput bound; we are bound by a model call that takes a second. |
| **mysql-connector-python** | Oracle's official driver. Heavier, its licensing is GPL-with-exception rather than plainly permissive, and its SQLAlchemy dialect is less exercised than PyMySQL's. |
| **asyncmy** | Async, fast, C-based. The connector is synchronous throughout — every other connector, the tool layer, and the agent are sync — and introducing async at one leaf would mean either an event loop inside a sync call or rewriting the layer above. |
| **No MySQL support; use a Postgres replica of each system** | Suggested more often than it is practical. It means running CDC or ETL from four production MySQL databases into Postgres, which is a larger and more fragile project than teaching one connector a second dialect. |

## Why pure Python is the right trade here

PyMySQL is slower than the C drivers on raw throughput. That does not matter:

- A query here is preceded by a model call that takes ~1 second. Saving 3ms on
  the driver is invisible.
- Results are capped at 1,000 rows by the safety layer, so there is no bulk
  transfer to optimise.
- `pip install pymysql` works identically on Windows, macOS, and Linux with no
  toolchain. That keeps `uv.lock` honest and keeps CI from needing apt packages.

Where performance would matter — a reporting workload pulling millions of rows —
this platform is the wrong tool anyway.

## The security consequence, and it is the point of this ADR

**MySQL has no `default_transaction_read_only`.**

Postgres layer 2 was a role-level setting making every connection start
read-only even if the code forgot to ask. MySQL has no equivalent. The
replacement is:

1. **`GRANT SELECT` and nothing else**, on curated views only. This is layer 1,
   unchanged, and it remains the actual control.
2. **`SET SESSION TRANSACTION READ ONLY`**, applied on every connection. Belt
   and braces — it catches a user that was provisioned with more rights than
   intended.

**Not via `init_command`, in the end.** The intended design was PyMySQL's
`init_command`, which takes exactly one statement — and two are needed, since
`max_execution_time` is set the same way. Neither the comma form
(`SET SESSION TRANSACTION READ ONLY, SESSION max_execution_time = 5000`) nor
the semicolon form parses.

The replacement is a SQLAlchemy `connect` event listener, which is better
regardless: the pool opens connections lazily and replaces dropped ones, so a
setting applied once after `create_engine` would cover the first connection and
silently miss every later one. `test_a_second_connection_also_starts_read_only`
exists for exactly that failure.

The test that proves this bypasses the validator and hands `INSERT`, `UPDATE`,
and `DELETE` straight to the connection, exactly as the Postgres test does. If
it ever passes, every other SQL safety test for MySQL is decoration.

## Consequences

**Positive:**

- The connector reaches MySQL, which is what the target systems run.
- The safety suite runs against a real MySQL in CI, so "the database refuses
  writes" becomes a tested claim for both engines rather than one.
- No build toolchain, no platform-specific wheels, no CI apt packages.

**Negative / accepted costs:**

- **Slower than the C drivers.** Immaterial at this workload; would matter at a
  different one.
- **A second dialect to keep correct.** `safety.py` now parses as either
  `postgres` or `mysql`, and a construction safe in one may not be in the
  other. The AST allowlist is shared, which limits the divergence, and the
  escapes corpus runs against both.
- **A second engine in docker-compose for tests.** MySQL on host port 3307,
  because 3306 is commonly taken on a development machine. CI uses 3306, where
  nothing competes for it; `MYSQL_PORT` is what keeps the tests agreeing with
  both.

## What a real MySQL found immediately

Two defects that no amount of reading would have surfaced, both invisible to a
mock:

- **`describe_schema` crashed against MySQL.** It read `row.table_name` from
  `information_schema.columns`, and MySQL returns those column names in
  UPPERCASE where Postgres returns lowercase. The method raised
  `AttributeError` — on the one call the agent makes *before* drafting any
  query, so a MySQL source would have failed at the first question asked of it.
  Fixed by aliasing the columns and unpacking positionally.

- **`SQLSettings` had no `engine` field.** Connectors built from a stored row —
  the only kind that exists in production, since integrations are created
  through the API — always defaulted to postgres. A MySQL integration was
  unreachable from the console no matter what an operator typed into it. The
  connector-level work was complete and the control plane could not reach it.

Both are recorded here because they are the argument for the container: the
dialect work looked finished, and the tests against a real engine disagreed.

## How we would remove it

The connector selects a dialect and a driver from `SQLConnectorConfig.engine`.
Removing MySQL support means deleting that branch, the test container, and one
line of `pyproject.toml`. `connectors/base.py` is untouched — the ABC has no
opinion about which database a SQL connector speaks, which is the property
`test_connector_abc_unchanged` has been pinning since Phase 5.
