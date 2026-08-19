# Enterprise AI Integration Platform

One governed AI platform with pluggable enterprise connectors — not N separate RAG systems.

- **Vision (future state):** [docs/ARCHITECTURE_VISION.md](docs/ARCHITECTURE_VISION.md)
- **Review & rationale:** [docs/ARCHITECTURE_REVIEW.md](docs/ARCHITECTURE_REVIEW.md)
- **Phase plan:** [docs/IMPLEMENTATION_ROADMAP.md](docs/IMPLEMENTATION_ROADMAP.md)
- **Working rules for AI sessions:** [CLAUDE.md](CLAUDE.md)

## Status

| Phase | State |
|---|---|
| **0 — Foundation** | ✅ Complete. Repo, pinned toolchain, Postgres 17 + pgvector 0.8.6, Redis, real `/health`, green CI |
| **1 — Tenant-scoped retrieval** | ✅ Complete. Schema + RLS, JWT identity, ingestion, authorization-filtered search |
| **2 — Grounded generation** | ✅ Complete. Cited answers, verified citations, refusal path, per-tenant cost |

**No LLM, no retrieval endpoint, no connectors, no frontend yet.** That is deliberate — see
the [roadmap](docs/IMPLEMENTATION_ROADMAP.md).

### Security guarantees currently enforced

These are tested on every push, not aspirations:

- Tenant isolation is a **database** guarantee, not application discipline. Row-Level
  Security is enabled *and forced* on `document`, `chunk`, and `app_user`.
- A query with no tenant context returns **zero rows**, not every row — default-deny.
- The runtime role (`app_rw`) is explicitly `NOSUPERUSER NOBYPASSRLS`. A superuser bypasses
  RLS unconditionally, so this property is what the whole guarantee rests on; a test asserts
  it, because every other isolation test still passes while isolation is silently gone.
- Generated SQL will use a read-only role enforced by Postgres, not by string inspection.
- **Tenancy is server-derived.** `tenant_id` comes from a verified token signature. A
  request supplying its own `tenant_id` in a body, query parameter, or header is ignored.
- Token verification pins the algorithm, so an `alg=none` token is rejected rather than
  trusted; issuer and audience are checked so a token minted for another service cannot
  be replayed here.
- Every authentication failure returns the same opaque 401 — the reason is logged
  server-side, never returned, so the endpoint is not a debugging oracle for an attacker.
- **Retrieval is authorization-filtered before ranking**, in the `WHERE` clause. Post-filtering
  would break top-k (ask for 5, get 2, with nothing saying why) and would mean the rows had
  already left the database — "retrieved then hidden" is not "not retrieved".
- A document with no labels is visible to **nobody**, and a user with no labels sees
  **nothing**. Default-deny in both directions.
- Traces record counts and lengths only — never query text or chunk content. A span table is
  a second copy of tenant data that nobody thinks to review.
- **Retrieved documents are framed as untrusted data**, structurally separated from
  instructions: the system prompt holds the rules, retrieved chunks go in the user message
  under `SOURCES`. A document containing "ignore previous instructions" is then text inside
  a section the model was told is data, not a competing instruction at the same level.
- **Citations are verified server-side.** Every `[n]` in an answer must resolve to a chunk
  actually retrieved for that request; the rest are stripped and logged.
- Provider errors never reach a client — they can carry request content. Timeouts return
  504, failures 502, both with a fixed message.
- **Listing is label-filtered too.** A user without the `finance` label does not learn that
  a finance document exists; a listing endpoint is an easy place to disclose the existence
  of data whose contents are protected everywhere else.
- Deleting a document a caller cannot see returns **404, not 403** — a 403 would confirm it
  exists. Deletion additionally requires the `admin` role: being able to read a document
  must not imply the authority to destroy it for everyone.

Run them yourself: `cd backend && uv run pytest -m security -v`

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker Desktop | latest | **Must be installed first** — see [Setting up Docker](#setting-up-docker-windows-11) |
| uv | 0.11+ | Python package manager. Already installed. |
| Python | 3.12 | uv installs it automatically from `backend/.python-version` |
| git | 2.x | Already installed |

You do **not** need to install Python 3.12 by hand. `uv sync` reads the pin and fetches it.

---

## Setting up Docker (Windows 11)

This is the one step that must happen outside the project. Budget ~30 minutes including reboots.

**1. Enable WSL2.** Open PowerShell **as Administrator** and run:

```powershell
wsl --install
```

Reboot when prompted. After rebooting, confirm:

```powershell
wsl --status      # should report Default Version: 2
```

If WSL was already present but on version 1: `wsl --set-default-version 2`

**2. Install Docker Desktop.** Download from https://www.docker.com/products/docker-desktop/
and run the installer. When asked, **keep "Use WSL 2 instead of Hyper-V" checked**.

**3. Start Docker Desktop** and wait for the whale icon in the system tray to stop animating.
In Settings → General, confirm *"Use the WSL 2 based engine"* is ticked.

**4. Verify** in a normal (non-admin) terminal:

```powershell
docker --version
docker compose version
docker run --rm hello-world
```

All three must succeed before continuing.

> **Licensing note:** Docker Desktop is free for personal use, education, and small
> businesses (under 250 employees and under $10M revenue). Larger organisations need a
> paid subscription. If your employer is over that threshold, use Rancher Desktop or
> Podman Desktop instead — the `docker compose` commands below are unchanged.

---

## First run

```powershell
# 1. Create your local environment file
copy .env.example .env
```

Then open `.env` and fill in every value. For local development these are fine:

```ini
APP_ENV=development
LOG_LEVEL=INFO

POSTGRES_USER=eaip
POSTGRES_PASSWORD=change-me-locally
POSTGRES_DB=eaip
POSTGRES_HOST=localhost
POSTGRES_PORT=5433

POSTGRES_READONLY_PASSWORD=change-me-too

REDIS_HOST=localhost
REDIS_PORT=6380
```

> **Ports 5433 and 6380 are intentional, not typos.** This machine runs another
> Docker project that publishes 5432, and Memurai (a Redis-compatible Windows service)
> on 6379. Publishing on 5433/6380 means neither stack has to be stopped for the other,
> and there is no startup-order race. Inside the containers the ports are still the
> standard 5432/6379 — only the host mapping differs. On a clean machine, the defaults
> are fine.
>
> **Redis port 6380, not 6379.** This machine runs Memurai (a Redis-compatible
> Windows service) natively on 6379. Mapping the container to 6380 lets both coexist,
> so you never have to stop a service to start the project. On a machine without
> Memurai, 6379 is fine.

> These are local-only credentials. Real environments get real secrets from a secret
> manager — never from a file. `.env` is gitignored and must stay that way.

```powershell
# 2. Start the infrastructure
docker compose up -d

# 3. Watch it become healthy (Ctrl+C to stop watching; containers keep running)
docker compose ps

# 4. Install Python dependencies
cd backend
uv sync

# 5. Run the API
uv run uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000/health**. You want:

```json
{"status":"ok","db":"ok","redis":"ok"}
```

Interactive API docs are at **http://127.0.0.1:8000/docs**.

---

## Verifying Phase 0 is actually done

`/health` returning `ok` proves connectivity. This proves the *database* is right:

```powershell
docker compose exec postgres psql -U eaip -d eaip -c "SELECT extversion FROM pg_extension WHERE extname='vector';"
```

Expect a `0.8.x` version. Then confirm the read-only role exists and really is read-only:

```powershell
docker compose exec postgres psql -U eaip -d eaip -c "\du app_readonly"
```

It must show **no** attributes such as Superuser or Create DB.

---

## Everyday commands

```powershell
# Infrastructure
docker compose up -d        # start
docker compose ps           # status
docker compose logs -f      # follow logs
docker compose down         # stop, keep data
docker compose down -v      # stop and DESTROY data (re-runs init.sh on next up)

# Backend (from backend/)
uv run uvicorn app.main:app --reload   # dev server
uv run pytest                          # tests
uv run ruff check .                    # lint
uv run ruff format .                   # format
uv run mypy app                        # type check
uv add <package>                       # add a dependency (needs an ADR)
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `docker: command not found` | Docker Desktop not installed or not running | Start Docker Desktop; wait for the tray icon to settle |
| `/health` shows `"db":"error"` | Containers not up, or `.env` mismatched | `docker compose ps`; check `POSTGRES_PORT` matches |
| `port 5432 already allocated` | Another Postgres is running | Already handled — we publish on `5433` |
| `port 6379 already allocated` | **Memurai is already running on this machine** | Either stop it (`Stop-Service Memurai`) or comment out the `redis` service in `docker-compose.yml` and use Memurai instead — it is Redis-compatible |
| `ValidationError` on startup | A variable is missing from `.env` | Compare against `.env.example` — every key needs a value |
| `vector` extension missing | Volume predates `init.sh` | `docker compose down -v; docker compose up -d` (destroys data) |

---

## Authentication (development)

There is no identity provider yet. Tokens are minted locally by the CLI, with
OIDC-shaped claims so a real IdP can replace the issuer later without changing the
verification path or the `Principal`.

```powershell
cd backend

# One-time setup
uv run alembic upgrade head
uv run python -m app.cli create-tenant acme "Acme Corp"
uv run python -m app.cli create-user acme alice@acme.test --roles reader --labels public,finance

# Mint a token (claims are read from the database, never from the command line)
uv run python -m app.cli token acme alice@acme.test
```

Then call the API with it:

```powershell
$token = uv run python -m app.cli token acme alice@acme.test
curl -H "Authorization: Bearer $token" http://127.0.0.1:8000/v1/me
```

`/v1/me` echoes the verified identity. Try supplying a different `tenant_id` as a
query parameter or header — the response will not change. Tenancy comes from the
token's signature, not from anything a caller can set.

## Ingesting documents

Only `.md` and `.txt` for now — PDF parsing arrives in Phase 3.

```powershell
cd backend
uv run python -m app.cli ingest ../sample-docs --tenant acme --labels public
```

`--labels` is required. A document with no labels is visible to nobody (default-deny),
so ingesting without them is almost always a mistake.

Ingestion is **idempotent by content hash**: re-running over an unchanged corpus creates
nothing. The hash is of file content, not path, so a renamed or moved file with identical
bytes is still one document. Duplicates are not merely wasteful — identical chunks crowd
each other out of the top-k, so a search returns the same answer five times instead of
five answers.

Embeddings come from OpenAI `text-embedding-3-small`, and every chunk records the model
and dimension that produced it. A corpus embedded with two different models is silently
unsearchable (cosine distance between vectors from different models is a meaningless
number, not an error), so the only defence is knowing which rows came from where.

**Tests never call OpenAI.** `FakeEmbeddings` is deterministic and offline, so the suite
costs nothing, needs no API key, and cannot flake on a network hiccup.

## Searching

```powershell
cd backend
$token = uv run python -m app.cli token acme alice@acme.test
curl -H "Authorization: Bearer $token" "http://127.0.0.1:8000/v1/search?q=how+long+for+a+refund"
```

There is **no tenant parameter** on this endpoint, and no way to add one. Tenancy and
permitted labels come from the token; supplying either as a query parameter or header
changes nothing.

The response echoes `filtered_by` — the caller's own tenant and labels, which they already
hold in their token. It never describes what was filtered *away*: that would leak the
existence of data they may not see. Every search returns a `trace_id`, and a matching row
lands in `trace_span`.

### Retrieval quality baseline

Golden set: **55 questions** — 34 semantic (paraphrased, sharing no words with the answer),
10 exact-identifier, 10 deliberately unanswerable.

| | R@1 | R@3 | R@5 | MRR |
|---|---|---|---|---|
| semantic | 0.88 | 1.00 | 1.00 | 0.931 |
| exact | 1.00 | 1.00 | 1.00 | 1.000 |
| **overall** | **0.91** | **1.00** | **1.00** | **0.947** |

```powershell
cd backend
uv run python ../evals/retrieval_eval.py   # needs OPENAI_API_KEY; costs a few cents
```

Recall@5 saturates on a corpus this size, so **Recall@1 is the number to watch** — and it is
the one that matters anyway, since the generator reads the top chunk first and its context
window is finite.

This is a script rather than a CI test on purpose: it uses real embeddings, so it costs
money and needs a key. The deterministic fake used in tests cannot measure semantic quality
by construction.

### Hybrid search is built, and turned off

`HYBRID_SEARCH_ENABLED=false`. Measured against the baseline above, adding keyword retrieval
made results **monotonically worse** — 0.91 → 0.68 overall Recall@1 at equal weighting.

The cause is worth knowing: table-aware chunking already keeps identifiers in a chunk that
also contains their column headings, so semantic search finds them without help. Exact-term
Recall@1 is already 1.00; hybrid has nothing to rescue, and its OR-matching drags in noise.

Full numbers and the conditions that would change the decision:
[docs/experiments/hybrid-search-comparison.md](docs/experiments/hybrid-search-comparison.md).
Chunk-size comparison: [docs/experiments/chunking-comparison.md](docs/experiments/chunking-comparison.md).

## Asking questions

```powershell
cd backend
$token = uv run python -m app.cli token acme alice@acme.test
$body = @{ question = "How long does a refund take?" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/v1/chat" `
  -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" -Body $body
```

Answers are generated **only** from retrieved chunks, and every citation is verified
server-side against the chunks actually retrieved for that request. A citation the model
invented is stripped before the response is sent and the drop is logged — a citation is
the part of an answer a reader trusts without checking, so an invented one produces
something that looks verified and is not.

When retrieval finds nothing relevant, the answer is `I don't have enough information to
answer that.` rather than a guess. If retrieval returns nothing at all, the model is not
called: generating without sources invites an answer from the model's own knowledge, which
is what grounding exists to prevent.

Set `"stream": true` for Server-Sent Events. Citations arrive in a final `done` event —
they cannot be verified until the sentence containing them has finished streaming.

### Cost

Every request records prompt tokens, completion tokens, and USD cost against a tenant and
user:

```sql
SELECT t.slug, count(*) AS answers, round(sum(m.cost_usd)::numeric, 4) AS usd
FROM conversation_message m JOIN tenant t ON t.id = m.tenant_id
WHERE m.role = 'assistant' GROUP BY t.slug;
```

Changing the model is a config edit (`LLM_MODEL` in `.env`), never a code change.

## Managing documents

```powershell
$token = uv run python -m app.cli token acme alice@acme.test

# List what you may see — filtered by your labels, not just your tenant
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/documents" -Headers @{ Authorization = "Bearer $token" }

# Delete (requires the admin role)
Invoke-RestMethod -Method Delete -Uri "http://127.0.0.1:8000/v1/documents/<id>" -Headers @{ Authorization = "Bearer $token" }
```

**Editing a file supersedes it.** Re-ingesting a changed document writes a new version
and tombstones the old one: the old row is retained so a citation issued last week still
resolves to the text that was actually cited, but it is excluded from retrieval so nobody
is answered from a stale revision.

**Deletion is immediate and complete** — chunks go in the same transaction, and the
document is unretrievable straight away. That is verified by a search in the test suite,
not by inspecting a table: "deleted but still answering" is a retention breach with a
paper trail saying it was deleted.

### Re-indexing after a model change

```powershell
uv run python -m app.cli reindex acme
```

Needed after changing `EMBEDDING_MODEL`. A corpus embedded with two models is silently
unsearchable — cosine distance between vectors from different models is a meaningless
number, not an error. Re-indexing touches vectors only; **labels are never modified**,
since a reindex that reset them would quietly widen who can see a document.

## Running alongside other Docker projects

Docker isolates Compose projects automatically — each gets its own network, volumes, and
container names. Two stacks only ever collide over **published host ports**.

This project therefore publishes deliberately non-default ports (`5433`, `6380`) so it can
coexist with anything else on the machine. To check what is taking a port:

```powershell
docker ps --format "table {{.Names}}	{{.Ports}}"
netstat -ano | findstr :5432
```

If you hit a conflict, change the port in `.env` and run `docker compose up -d` again.
Your data lives in a named volume and survives the change.

## Project layout

```
CLAUDE.md               Working rules for AI coding sessions
docker-compose.yml      Postgres 17 + pgvector, Redis 7
.env.example            Every variable, no values
infra/postgres/init.sh  Extension + read-only role (runs once)
backend/
  app/
    main.py             FastAPI entry point
    core/config.py      Environment settings, validated at import
    db/session.py       Engine, session factory, Redis client
    api/health.py       Dependency-checking health endpoint
  alembic/              Migrations (none yet)
  tests/
docs/                   Vision, review, roadmap, ADRs
.github/workflows/      CI: ruff, mypy, pytest
```
