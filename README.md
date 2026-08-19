# Enterprise AI Integration Platform

One governed AI platform with pluggable enterprise connectors — not N separate RAG systems.

- **Vision (future state):** [docs/ARCHITECTURE_VISION.md](docs/ARCHITECTURE_VISION.md)
- **Review & rationale:** [docs/ARCHITECTURE_REVIEW.md](docs/ARCHITECTURE_REVIEW.md)
- **Phase plan:** [docs/IMPLEMENTATION_ROADMAP.md](docs/IMPLEMENTATION_ROADMAP.md)
- **Working rules for AI sessions:** [CLAUDE.md](CLAUDE.md)

## Status: Phase 0 — Foundation

A repository, a pinned toolchain, Postgres + pgvector, Redis, and a real `/health`.
**No LLM, no retrieval, no connectors, no frontend yet.** That is deliberate — see the roadmap.

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
POSTGRES_PORT=5432

POSTGRES_READONLY_PASSWORD=change-me-too

REDIS_HOST=localhost
REDIS_PORT=6379
```

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
| `port 5432 already allocated` | A local Postgres is already running | Change `POSTGRES_PORT` in `.env` to `5433` |
| `port 6379 already allocated` | **Memurai is already running on this machine** | Either stop it (`Stop-Service Memurai`) or comment out the `redis` service in `docker-compose.yml` and use Memurai instead — it is Redis-compatible |
| `ValidationError` on startup | A variable is missing from `.env` | Compare against `.env.example` — every key needs a value |
| `vector` extension missing | Volume predates `init.sh` | `docker compose down -v; docker compose up -d` (destroys data) |

---

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
