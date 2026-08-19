# CLAUDE.md — Enterprise AI Integration Platform

Working instructions for AI coding sessions. The architecture *vision* lives in
[docs/ARCHITECTURE_VISION.md](docs/ARCHITECTURE_VISION.md) — that is aspirational future state,
not current state. This file is current state.

## Current phase

**Phase 0 — Foundation.** Repo, toolchain, Postgres+pgvector, Redis, `/health`, CI.
There is no LLM, no retrieval, no connector, and no frontend yet. Do not add them.
See [docs/IMPLEMENTATION_ROADMAP.md](docs/IMPLEMENTATION_ROADMAP.md) for what each phase unlocks.

## How to run

```powershell
docker compose up -d          # Postgres 17 + pgvector, Redis 7
cd backend
uv sync                       # installs into .venv from uv.lock
uv run uvicorn app.main:app --reload
```

Health check: http://127.0.0.1:8000/health — must report `db: ok` and `redis: ok`.

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
   string inspection. Write operations are forbidden until Phase 9.
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
- Reintroduce `Source Code 2/` — it was a fork; this directory is the only repo root.
