# Runbook

Operating the platform: routine tasks, scheduled work, and what to do when
something is wrong.

Commands assume PowerShell from the repository root unless stated otherwise.

---

## Starting and stopping

```powershell
docker compose up -d          # Postgres 17 + pgvector, Redis
cd backend
uv run uvicorn app.main:app --reload

cd ../frontend                # second terminal
npm run dev                   # http://localhost:3000
```

**Health check:** http://127.0.0.1:8000/health — must report `db: ok` and
`redis: ok`. If either is not `ok`, nothing else in this runbook will work.

**Host ports are deliberately non-default** — Postgres `5433`, Redis `6380`.
Another project and a native Memurai service hold 5432 and 6379 on this machine.
Container-internal ports are unchanged.

```powershell
docker compose down           # stop, keep data
docker compose down -v        # stop and DESTROY the volume — see "Restoring"
```

---

## Routine tasks

### Adding a person

Console → **Users** → Add. Or:

```powershell
cd backend
uv run python -m app.cli create-user acme person@acme.test --roles reader --labels public
```

Labels are the permission system. **An empty label set means they can retrieve
nothing** — that is default-deny, not a mistake.

### Issuing a token

```powershell
uv run python -m app.cli token acme person@acme.test
```

Paste it into the console's login page. Tokens last 60 minutes.

There is no password because there is no identity provider yet. The login page
says so rather than implying a credential check that does not happen. Phase 11
replaces this.

### Ingesting documents

```powershell
uv run python -m app.cli ingest ..\sample-docs --tenant acme --labels public
```

Idempotent by content hash: re-running on an unchanged corpus is a no-op.
Re-running after an edit supersedes the old revision rather than deleting it, so
a citation issued last week still resolves.

**Check the PII badge afterwards.** Console → Knowledge. A document showing
**⚠ 47 PII** is telling you to think about its label before someone asks a
question that retrieves it.

### Adding a connector

Console → **Integrations** → Add. You need:

- host, port, database name
- a **read-only** database user — see below
- that user's password

Then **Test**. Green means it connected.

**Creating the read-only user is the important part.** On the source system:

```sql
CREATE ROLE reporting_readonly LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
CREATE SCHEMA curated;
CREATE VIEW curated.v_orders AS SELECT ... FROM orders;
GRANT USAGE ON SCHEMA curated TO reporting_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA curated TO reporting_readonly;
```

Grant `SELECT` on **curated views only**, never on base tables. That grant is
the actual security boundary — everything the platform does in front of it is a
fast filter, not the control.

### Checking spend

```powershell
curl.exe -s http://127.0.0.1:8000/v1/me/quota -H "Authorization: Bearer $tok"
```

Default ceiling is $5.00 per tenant per UTC day, set by
`TENANT_DAILY_BUDGET_USD`. Over budget returns **402**, not 429 — retrying a 429
succeeds shortly, retrying a 402 does not.

---

## Scheduled work

Nothing below runs automatically. In a real deployment these are cron jobs; on
a workstation they are things to remember.

### Retention — daily

```powershell
cd backend
uv run python -m app.cli retention --dry-run    # see what would go
uv run python -m app.cli retention              # actually delete
```

Deletes rows past their retention period. Batched at 10,000 per table per run,
so a first run against a large backlog cannot hold locks for minutes — run it
again until it reports nothing.

Periods are in [DATA_POLICY.md](DATA_POLICY.md).

### Backup — daily

See *Backup and restore* below.

### Dependency scan — weekly, and it runs in CI

```powershell
cd backend
uv export --frozen --no-dev --no-emit-project > reqs.txt
uv run --with pip-audit pip-audit --disable-pip --requirement reqs.txt
Remove-Item reqs.txt
```

This is worth running between CI builds because advisory data changes daily. Its
first run found four CVEs in our LangGraph checkpoint packages, two of them RCE
in checkpoint deserialisation, which is directly on the agent path. Nothing had
told us because nothing was looking.

---

## Backup and restore

> **Not yet built.** The scripts and the restore test below are Phase 8 stage 5.
> This section describes what they will do; until then, `pg_dump` by hand.

**An untested backup is not a backup.** The restore procedure below will be
exercised by `test_restore_from_backup`, so it is known to work rather than
believed to.

### Taking one

```powershell
.\infra\backup\backup.ps1
```

Writes a timestamped custom-format dump to `infra/backup/dumps/`.

### Restoring

```powershell
.\infra\backup\restore.ps1 -DumpFile infra\backup\dumps\eaip-20260820-120000.dump -Database eaip_restored
```

Restores into a **new** database by default. Overwriting the live one requires
naming it explicitly, because a restore is the operation most likely to be run
in a hurry.

### What recovery actually costs

The dump contains embeddings, so a restore does **not** re-embed. That matters:
re-embedding a corpus is an API bill and an hour, not a `pg_restore`.

But a restore from a dump **older than a corpus change** means re-ingesting the
documents added since — and *that* re-embeds. Recovery time is therefore:

```
restore time  +  (documents ingested since the dump) × embedding time
```

Take a dump after any large ingestion, not just on a schedule.

---

## Rotating secrets

### `JWT_SECRET`

Rotating invalidates every issued token. Nobody is logged out silently — they
get a 401 and sign in again.

1. Set the new value in `.env`
2. Restart the backend
3. Tell people to sign in again

No data migration. Tokens are stateless.

### `CREDENTIAL_ENCRYPTION_KEY`

**This one needs care.** Stored connector credentials are encrypted with it, and
changing it without re-encrypting makes every connector fail to decrypt — which
surfaces as "stored credential could not be decrypted" on Test Connection.

The safe procedure, because there is no automated re-encryption path yet:

1. Note which connectors exist and have credentials (Console → Integrations)
2. Have the source-system passwords to hand — **they cannot be read back from
   this platform**, by design
3. Change the key in `.env`, restart
4. Re-enter each connector's credential through the console
5. **Test** each one

Rehearse this on a copy before doing it live. The failure is loud rather than
silent, but it is still an outage of every connector until step 4 is finished.

### Database passwords

`POSTGRES_PASSWORD`, `POSTGRES_APP_PASSWORD`, `POSTGRES_READONLY_PASSWORD`:

```sql
ALTER ROLE app_rw WITH PASSWORD 'new-value';
```

Then update `.env` and restart. The roles are cluster-wide, so a stale password
on a second database is a real failure mode — this bit us once.

---

## Troubleshooting

### "Not authenticated" (401)

The token expired (60 minutes) or the signing key changed. Mint a new one.

The message is deliberately identical for every auth failure so it cannot be
used as an oracle. That is why it does not say which.

### "Requires the admin role" (403)

The account has `reader`, not `admin`. Console → Users → Edit → tick
Administrator. Note that the **nav link stays visible** — hiding it would be a
courtesy that reads like a control.

### Chat returns "I don't have enough information"

Working correctly, in one of two ways:

1. **Nothing was retrieved.** Check the person's labels against the document's
   labels — they must overlap. This is by far the common case.
2. **Nothing relevant was retrieved.** The corpus genuinely does not answer it.

A refusal is a feature. The alternative is a plausible invention.

### An edit in the console fails with a 400

Check the browser's network tab for a failed **OPTIONS** request. If the
preflight returned "Disallowed CORS method", an endpoint is using an HTTP method
missing from `allow_methods` in `main.py`.

This happened with PATCH and was invisible to every curl test — curl sends no
preflight. `test_cors.py` now derives the required methods from the OpenAPI
schema, so a new verb fails there instead of in a browser.

### "Too many requests" (429)

Rate limited. Test Connection is 5/minute/tenant; model endpoints are
`LLM_REQUESTS_PER_MINUTE` (default 30). The `Retry-After` header says when.

**The rate limiter fails closed** — if Redis is down, requests are denied. That
is deliberate: it guards an SSRF primitive, and an outage is exactly when an
attacker benefits from it being open.

### "Daily budget exhausted" (402)

The tenant hit `TENANT_DAILY_BUDGET_USD`. Resets at midnight UTC. Raise it in
`.env` and restart if it is genuinely too low.

**The quota fails open** — if Redis is down, requests proceed. The opposite of
the rate limiter, deliberately: this guards a bill, and denying every request
because a cache is down turns a billing control into an outage.

### The agent used one tool when it should have used two

Check the tool list at the bottom of the agent's answer. If a connector is
missing, it was not registered — usually an egress block. Look for
"blocked by egress policy" in the backend log and set
`ALLOW_LOOPBACK_CONNECTORS=true` for local development.

This exact bug once made the agent silently invent a `database_query` tool. The
egress error was being swallowed by a bare `except Exception`.

### Connector test says "address not permitted by egress policy"

Working as intended. Link-local (`169.254.x.x`) is blocked unconditionally.
Private ranges and loopback need per-connector opt-in — the checkboxes in the
connector form.

### Tests fail with "no database reachable"

`docker compose up -d`, then check `/health`. Security tests **skip** rather
than fail when the database is absent, which is a deliberate trade — but a
skipped security test proves nothing, so treat a skip in CI as a failure.

---

## Known gaps

Recorded so they are decisions rather than surprises.

| Gap | Consequence | Where it goes |
|---|---|---|
| Orphaned LangGraph checkpoints | Deleting a tenant leaves checkpoint rows behind; they carry no `tenant_id` | Needs a purge by `thread_id` |
| No automated key re-encryption | Rotating `CREDENTIAL_ENCRYPTION_KEY` means re-entering credentials by hand | Phase 11 |
| Retention is manual | Data accumulates until someone runs the command | Schedule it |
| Tokens in `sessionStorage` | XSS could read one | Phase 11, with httpOnly cookies |
| No identity provider | Tokens minted by CLI | Phase 11 |

---

## Related

- [THREAT_MODEL.md](THREAT_MODEL.md) — what each control defends against
- [DATA_POLICY.md](DATA_POLICY.md) — what leaves, and how long we keep it
- [IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md) — what each phase unlocks
