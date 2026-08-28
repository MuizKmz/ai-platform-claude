# CLAUDE.md — Enterprise AI Integration Platform

Working instructions for AI coding sessions. The architecture *vision* lives in
[docs/ARCHITECTURE_VISION.md](docs/ARCHITECTURE_VISION.md) — that is aspirational future state,
not current state. This file is current state.

## Current phase

**Phase 11 complete, deployed to production. Phase 12 in progress.**
See [docs/CHAT_WIDGET_SDK.md](docs/CHAT_WIDGET_SDK.md) and
[docs/SCHEMA_DISCOVERY_WIZARD.md](docs/SCHEMA_DISCOVERY_WIZARD.md) for what
Phase 12 is. Everything below records how each phase up to 11 was reached —
kept because the reasoning in it is still load-bearing, not because the
project is still at an earlier phase.

**Phase 12 — the embeddable chat widget SDK is built.** Three packages under
[sdk/](sdk/): `eaip-client` (framework-agnostic, talks only to the host app's
own backend), `eaip-widget` (`<EaipChat />` + `useEaipChat`, self-contained
styling), `eaip-proxy-endpoint` (the copy-paste backend snippet — holds the
service-account secret, mints and caches a token, forwards MCP calls
server-to-server). 44 tests against fakes of Keycloak and `/mcp`.

The plan's first draft had the browser call `/mcp` directly with a short-lived
token. That fails from a browser on an integrator's own domain: `CORS_ORIGINS`
is a single origin and `/mcp` inherits the global CORS middleware, so the
preflight is refused. (`/mcp` is publicly routed — that was briefly thought to
be a second blocker and isn't.) So the host backend proxies the tool calls too,
reached server-to-server where there is no `Origin` — EAIP needs no CORS change
and no per-integrator config, and the token never reaches the browser. Do not
add a browser-to-EAIP path or a per-origin CORS allowlist; the proxy shape is
the design.

**Still open in Phase 12:** the SETUP.md smoke run against the live endpoint
with a real `eaip-mcp` credential, and migrating the IoT integration onto the
widget as the first real consumer. The schema-discovery wizard
([docs/SCHEMA_DISCOVERY_WIZARD.md](docs/SCHEMA_DISCOVERY_WIZARD.md)) is a
separate, not-yet-built piece of the same phase.

**Phase 5 complete — two connectors, one interface.** Everything through grounded generation, plus
PDF/DOCX/HTML ingestion, document lifecycle, a background worker, and a read-only SQL
connector whose write-refusal is enforced by a Postgres role rather than by inspecting
strings.

**Phase 6 complete — the control plane.** Connectors moved out of code into a table
with RLS and encrypted, write-only credentials; an integrations API and page; users,
roles, and labels; the trace and audit viewers; and Playwright E2E (ADR 0006).

**Phase 7 complete — agent orchestration.** LangGraph loop over three tool types, with
hard limits, invocation-time authorization, and Postgres checkpointing (ADR 0005).

**Credentials are write-only.** No endpoint returns one — not masked, not to admins.
`ConnectorOut` has no field for it; `has_credential` is the boolean a UI needs. Adding
a read path would undo the guarantee, not extend it.

**Phase 8 complete — hardening.** Per-tenant rate limits and daily budgets, PII detection
at ingestion with redaction in traces, data retention, a 37-attack red-team corpus in CI
with zero successful escalations, dependency scanning, and a backup whose restore is
actually performed. Security docs: [THREAT_MODEL](docs/THREAT_MODEL.md),
[DATA_POLICY](docs/DATA_POLICY.md), [RUNBOOK](docs/RUNBOOK.md).

**Phase 9 complete — governed writes.** The agent can *propose* an action; it cannot
perform one. A `WriteTool` has no execution code at all — `run()` records a pending
`approval_request` and returns. Execution lives in `tools/approval.py`, which only
`api/v1/approvals.py` may import, enforced by a test that scans the import graph.

Write tools are **off by default** (`ENABLED_WRITE_TOOLS` is empty). Proposing needs the
`analyst` role; approving needs `admin`, and a requester cannot approve their own request.
Writes go through business APIs only — never generated SQL, and Phase 4's read-only role
is unchanged.

Do not add an autonomous write path, a "trusted agent" tier, or bulk operations. If a
change makes `execute_approved` reachable from the agent, the phase's guarantee is gone.

**Phase 10 complete — MCP at the edge.** A second front door onto the same tools, spoken
as JSON-RPC over HTTP (ADR 0007, implemented directly rather than with the SDK).

*One chokepoint, two front doors.* `app/mcp/server.py` calls
`registry.invoke(principal, name, **arguments)` — the same function the agent calls, not
a copy of it. A surface with its own authorization is a second security model.

MCP has its **own audience** (RFC 8707): a console token is refused by the MCP server and
vice versa. The token is verified, used to derive a Principal, and dropped — never
attached to anything outbound. Write tools are excluded by TYPE, not by name, because an
MCP client has no approval queue.

**Phase 11 in progress — identity is done, the rest is not.** A real identity provider
(Keycloak, ADR 0009) replaced the CLI token issuer. Tokens are verified with RS256 against
the provider's published keys, so **this process can check an identity and can no longer
mint one** — the symmetric key did both, and reading it was enough to forge an admin token
for any tenant.

The `Principal` did not change and no endpoint, policy, or authorization check was touched.
Phase 1 shaped the claims for this, and that bet paid.

`issue_token` still exists for the test suite and refuses to run when `APP_ENV=production`
and a provider is configured. A credential-free issuer left reachable beside a real one
keeps the weakest path open. `tests/conftest.py` neutralises OIDC for the same reason in
reverse — the suite mints its own HS256 tokens, and a developer whose `.env` points at
Keycloak should not watch 137 tests fail.

Keycloak's database is a **separate backup obligation**. A restore that recovers EAIP but
not Keycloak recovers a platform nobody can log into.

**Phase 11 complete — deployed, not just designed.** EAIP runs in production at
`https://aiplatform.clbgroups.com`, on the aaPanel/Huawei host, behind aaPanel's own
Nginx reverse proxy terminating real TLS (Let's Encrypt). `infra/DEPLOY-aapanel.md`
describes a deployment that **has been performed** — the doc was rewritten mid-deployment
once the plan changed from an SSH-tunnel pilot to a public domain, and again as real bugs
surfaced only by actually running it: `start`, not `start --optimized`, on first
Keycloak boot; `--import-realm` had to be spelled out again since `start` doesn't imply it
the way the dev file's `start-dev --import-realm` does; and this specific host cannot bind
a Docker-published port to `127.0.0.1` (a kernel/nftables interaction, not a config
mistake), so `docker-compose.prod.yml` binds `0.0.0.0` and relies on the cloud provider's
Security Group instead — verified externally unreachable before being trusted, not
assumed. A real tenant, a real admin user, and the IoT MariaDB connector are live; MCP
was proven working end-to-end with a real token against the real `eaip-mcp` client.

Two Postgres databases (EAIP's own, and Keycloak's) are backed up daily via cron and
**restore-verified** — each dump was actually restored into a disposable container and
its tables queried, not just assumed to work because the dump command exited zero.

**Still genuinely open:** monitoring and alerting (nothing pages anyone if a process
crashes at 3am — aaPanel's process managers restart it, but nobody is told), a *rehearsed*
restore of the whole stack together rather than one database restore proven in isolation,
a load test, and getting backups copied off the VM itself rather than living only on the
disk they protect against. None of these block real use; all of them are real gaps.

**MySQL is supported alongside Postgres** (ADR 0008). The ERP, WMS, MES, and IoT systems
this platform serves all run MySQL, so proving the safety layers against Postgres alone
proved them for an engine nobody uses. The suite now runs against a real MySQL in CI.

`SQLSettings.engine` selects the dialect and defaults to `postgres`, so a connector row
written before the field existed keeps its meaning. The AST allowlist is **shared** between
dialects — a construction permitted for one is permitted for both, which is what stops a
statement being safe in Postgres and unexamined in MySQL.

Tool authorization is re-checked at every invocation in `tools/base.py`. Never cache it,
never trust what the model requested, and never let a tool be invoked outside
`ToolRegistry.invoke`.

The frontend is a **pure API client**. All backend calls go through
`frontend/src/lib/api.ts`; business logic in a route handler means two backends,
and the authorization guarantees live behind the API where RLS enforces them.

`connectors/base.py` is pinned by `test_connector_abc_unchanged`. Two implementations
now share it unmodified; if a third requires editing it, reconsider the design rather
than updating the hash.
See [docs/IMPLEMENTATION_ROADMAP.md](docs/IMPLEMENTATION_ROADMAP.md) for what each phase unlocks.

## How to run

This section is local development only. **Production is live** at
`https://aiplatform.clbgroups.com` — see [infra/DEPLOY-aapanel.md](infra/DEPLOY-aapanel.md)
for how it's actually deployed and run there; do not treat anything below as describing it.

```powershell
docker compose up -d          # Postgres 17 + pgvector, Redis 7, MySQL 8.4, Keycloak
cd backend
uv sync                       # installs into .venv from uv.lock
uv run uvicorn app.main:app --reload

cd ../frontend                # in a second terminal
npm install
npm run dev                   # http://localhost:3000
```

Health check: http://127.0.0.1:8000/health — must report `db: ok` and `redis: ok`.

**Host ports on this machine are deliberately non-default** — Postgres `5433`, Redis `6380`,
MySQL `3307`, Keycloak `8081`. Another project and a native Memurai service hold 5432 and
6379, 3306 is commonly taken by a local MySQL, and 8080 is the most contested port on any
development machine. Container-internal ports are unchanged; only the published host port
differs. Do not "helpfully" reset these.

**Signing in** depends on whether `OIDC_JWKS_URL` is set. Empty, and the console shows the
paste-a-token form and `python -m app.cli token` mints one. Set, and the console redirects
to Keycloak for a real password. See [infra/keycloak/README.md](infra/keycloak/README.md)
for creating a user — note that a user needs `tenant_id` and `labels` attributes and a
realm role, and that a user missing `tenant_id` is **refused**, not defaulted.

MySQL takes about 70 seconds to become healthy on a first start, because the entrypoint
initialises the data directory before running `infra/mysql/init.sql`. `uv run pytest` skips
the MySQL tests if it is not up yet — CI fails the build on that skip, but locally you just
get a quieter run than you meant, so check `docker compose ps` if the count looks low.

## How to test

```powershell
cd backend
uv run pytest                 # requires docker compose to be up
uv run ruff check .
uv run ruff format --check .
uv run mypy app
```

CI runs all four on every push. A red CI blocks the next phase.

```powershell
cd frontend
npm run e2e                   # Playwright; needs all three services up
```

E2E is separate because it needs the whole stack running. Run it after any change
that touches the API surface or a form — it covers the browser-only failures nothing
else can see (a missing CORS method fails the preflight while every curl succeeds).
See `frontend/e2e/README.md`.

## The layering rule

Dependencies point **downward only**. No upward imports, no lateral imports between
sibling capability modules.

```
api/ → agent/ → tools/ → connectors/ → external systems
api/ → knowledge/
```

- `connectors/` must never import `tools/`, `agent/`, or `api/`.
- `agent/` must never import `api/`.
- `core/`, `db/`, `observability/` are cross-cutting: anyone may import them; they import
  nothing but each other.

## Security invariants

These are not features and are never deferred to "later". Violating one is a blocking bug.

1. **Tenancy is server-derived.** `tenant_id` comes from the verified identity, never from a
   request body, query param, header, or LLM output.
2. **Every row of tenant data carries `tenant_id`.** Every query filters on it. No exceptions.
3. **Retrieval is authorization-filtered before ranking**, not after. Never retrieve then hide.
4. **Database access for generated SQL is read-only**, enforced by a database grant, not by
   string inspection. Generated SQL never writes — Phase 9's writes go through business
   APIs behind approval, never through this path. The test that proves this bypasses the
   AST validator entirely; if it ever passes, every other SQL safety test is decoration.
   It exists for **both** engines: `test_sql_safety.py` and `test_mysql_safety.py`.

   The engines enforce it differently. Postgres has a role with `SELECT` plus
   `default_transaction_read_only`. **MySQL has no such role flag** — the grant alone is
   the control, backed by a per-connection `SET SESSION TRANSACTION READ ONLY` applied by
   a `connect` event listener (ADR 0008).
5. **No secrets in git, logs, traces, prompts, or responses.** `.env` is gitignored; use
   `.env.example` as the template.
6. **Every LLM/tool/retrieval call emits a trace** with a trace ID, latency, and token cost.

## Conventions

- Python 3.12, pinned via `backend/.python-version`. Do not use 3.14 — wheel coverage is poor.
- `uv` is the package manager. Commit `uv.lock`. Never `pip install` into the venv.
- Every new external dependency needs an ADR in `docs/adr/` — why, what it replaces, how to
  remove it. Template: `docs/adr/0000-template.md`.
- Migrations via Alembic. Never edit a schema by hand.

## Do not

- Add anything from the vision doc's technology table "because it's on the list". Each arrives
  in the phase that needs it, with an ADR.
- Build write-capable tools, multi-agent systems, or Kubernetes config.
- Touch `../Source Code 2/`. It is a separate project with its own Docker stack, volume,
  network, and database. It is fully isolated from this one and is none of our business.
  Do not read it for guidance, copy from it, or stop its containers.
