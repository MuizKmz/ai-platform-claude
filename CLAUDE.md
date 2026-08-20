# CLAUDE.md — Enterprise AI Integration Platform

Working instructions for AI coding sessions. The architecture *vision* lives in
[docs/ARCHITECTURE_VISION.md](docs/ARCHITECTURE_VISION.md) — that is aspirational future state,
not current state. This file is current state.

## Current phase

**Phase 5 complete — two connectors, one interface.** Everything through grounded generation, plus
PDF/DOCX/HTML ingestion, document lifecycle, a background worker, and a read-only SQL
connector whose write-refusal is enforced by a Postgres role rather than by inspecting
strings.

**Phase 7 complete — agent orchestration.** LangGraph loop over three tool types, with
hard limits, invocation-time authorization, and Postgres checkpointing (ADR 0005).

There are still **no write operations**. Do not add any — that is Phase 9, and it needs
the approval model that phase specifies. Phase 8 is security and reliability hardening.

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

```powershell
docker compose up -d          # Postgres 17 + pgvector, Redis 7
cd backend
uv sync                       # installs into .venv from uv.lock
uv run uvicorn app.main:app --reload

cd ../frontend                # in a second terminal
npm install
npm run dev                   # http://localhost:3000
```

Health check: http://127.0.0.1:8000/health — must report `db: ok` and `redis: ok`.

**Host ports on this machine are deliberately non-default** — Postgres `5433`, Redis `6380`.
Another project and a native Memurai service hold 5432 and 6379. Container-internal ports
are unchanged; only the published host port differs. Do not "helpfully" reset these.

## How to test

```powershell
cd backend
uv run pytest                 # requires docker compose to be up
uv run ruff check .
uv run ruff format --check .
uv run mypy app
```

CI runs all four on every push. A red CI blocks the next phase.

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
4. **Database access for generated SQL is read-only**, enforced by a Postgres role, not by
   string inspection. Write operations are forbidden until Phase 9. The test that proves
   this bypasses the AST validator entirely — if it ever passes, every other SQL safety
   test is decoration.
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
