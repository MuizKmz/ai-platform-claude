# Deploying to the aaPanel host

> **STATUS: PERFORMED.** EAIP is live at `https://aiplatform.clbgroups.com` on
> this host, following the steps below. This document was rewritten during
> that deployment, twice: once when the plan changed from an SSH-tunnel pilot
> to a public domain (adding TLS, the reverse proxy, and process managers),
> and again as real bugs surfaced that only showed up by actually running each
> step — several are called out inline below with what broke and why, because
> they're the kind of thing worth knowing before hitting them again on a
> second host.
>
> A developer's local machine (backend, frontend, Postgres, Redis, Keycloak
> in local Docker, reaching the IoT MariaDB over an SSH tunnel) remains the
> normal way to develop against this codebase day to day — this document only
> covers the separate, additional production deployment.

The target is the Huawei Cloud VM that already runs the IoT platform, a WMS,
and around a dozen client databases under aaPanel.

**The governing constraint is that none of those may be disturbed.** Everything
below follows from it. If a step here would affect an existing service, that is
a bug in this document, not an acceptable cost. Concretely: ports 80 and 443
belong to aaPanel's Nginx already — EAIP joins it as a site with a reverse
proxy, it does not take those ports for itself.

---

## What this does and does not touch

| Touched | Not touched |
|---|---|
| A new directory under `/opt/eaip` | `/www/wwwroot/*` — every existing site's files |
| One new aaPanel site + reverse proxy, for `aiplatform.clbgroups.com` | Every other aaPanel site and its Nginx config |
| Four Docker containers (Postgres, Redis, Keycloak, Keycloak's DB) | The host's MariaDB service |
| Host ports 5433, 6380, 8081 — **loopback only** | Ports 80, 443, 3306, and aaPanel's own |
| Two aaPanel-managed processes (backend on 8000, frontend on 3000), also loopback-only | Every other process aaPanel already manages |
| A new MariaDB database `iot_curated` (views) | `iot_db` and every other database |
| A new MariaDB user `eaip_readonly` | Every existing MariaDB user |

Nothing in an existing site's document root is read, written, or moved.

---

## The URL shape

One domain, one certificate, path-based routing to three different loopback
services:

```
https://aiplatform.clbgroups.com/           -> frontend   (127.0.0.1:3000)
https://aiplatform.clbgroups.com/v1/*       -> backend    (127.0.0.1:8000)
https://aiplatform.clbgroups.com/health     -> backend    (127.0.0.1:8000)
https://aiplatform.clbgroups.com/realms/*   -> keycloak   (127.0.0.1:8081)
https://aiplatform.clbgroups.com/admin/*    -> keycloak   (127.0.0.1:8081)
https://aiplatform.clbgroups.com/resources/* -> keycloak  (127.0.0.1:8081)
```

`/v1` and `/health` are the backend's actual route prefixes
(`backend/app/main.py`'s routers all mount under `/v1`; only the health check
is unprefixed) — not an invented `/api` layer. `frontend/src/lib/api.ts` calls
`${API_BASE}/v1/...` with `API_BASE` as the bare origin, so the proxy must
forward these paths through unchanged, not strip or rewrite them.

The `/realms`, `/admin`, and `/resources` paths are Keycloak's own — that's
where its login pages, token endpoint, JWKS, and admin console live. Anything
not matching one of those goes to Keycloak's default realm resources handler,
which is why `/resources/*` needs its own rule rather than falling through.

This is why `KC_HOSTNAME` in `docker-compose.prod.yml` is
`https://aiplatform.clbgroups.com` with no path prefix — Keycloak generates
absolute URLs under that host for `/realms/...`, and the proxy rule above is
what makes those actually resolve.

---

## Before you start

**1. The IoT team should know.** A read-only user plus a views database on their
production server is a small change, but it is their server. Get the nod.

**2. Check there is room.**

```bash
free -m          # need ~2 GB free; the containers are capped at ~2.3 GB total
df -h /          # need ~5 GB
docker --version # if this fails, see "Installing Docker" below
```

If `free -m` shows under 2 GB available, stop and say so — the limits in
`docker-compose.prod.yml` need lowering, and that is a deliberate decision
rather than something to squeeze. Keycloak's own container adds roughly 512 MB
over the previous plan's Postgres/Redis-only footprint.

**3. Confirm the host's ports are free.** These are the ones EAIP wants:

```bash
ss -lntp | grep -E ':(3000|8000|5433|6380|8081)\b'
```

Empty output is what you want. Anything listed means a port collision, and the
fix is to change EAIP's port in `.env` — never to stop whatever is already
there.

**4. Confirm DNS actually points here** before requesting a certificate for it:

```bash
nslookup aiplatform.clbgroups.com 8.8.8.8
```

Must return this host's public IP. Let's Encrypt's validation will fail
otherwise, and aaPanel will report that failure without necessarily saying why.

---

## Installing Docker

Skip if `docker --version` already worked.

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
```

This installs Docker's own repository and does not alter aaPanel, PHP, Nginx,
or MariaDB. It does add iptables rules — see the port-binding warning below,
which is why every published port in this deployment is loopback-bound.

---

## Step 1 — the curated views

Run this FIRST. If it fails, nothing else is worth doing.

```bash
# From your laptop:
scp infra/mysql/iot-curated.sql root@<host>:/root/

# On the server:
nano /root/iot-curated.sql
#   change  CHANGE-ME-BEFORE-RUNNING  to a real password
#   keep that password; it goes in the console as a connector credential later
#   do not paste it into a chat, a ticket, or this file

mysql -uroot -p < /root/iot-curated.sql
```

**Verify before continuing.** The grant is the entire security boundary:

```bash
mysql -uroot -p -e "SHOW GRANTS FOR 'eaip_readonly'@'localhost'"
```

Expected — exactly two lines:

```
GRANT USAGE ON *.* TO `eaip_readonly`@`localhost` IDENTIFIED BY PASSWORD '...'
GRANT SELECT ON `iot_curated`.* TO `eaip_readonly`@`localhost`
```

`USAGE ON *.*` means *no privileges*, not *some privileges*. **Any third line —
especially one naming `iot_db` — means stop.** The user can reach more than it
should.

Then confirm the boundary by trying to cross it:

```bash
mysql -ueaip_readonly -p -e "SELECT COUNT(*) FROM iot_curated.v_devices"   # works
mysql -ueaip_readonly -p -e "SELECT COUNT(*) FROM iot_db.users"            # must FAIL
mysql -ueaip_readonly -p -e "DELETE FROM iot_db.device_metrics"            # must FAIL
```

The second and third failing is the point. If either succeeds, stop and tell me.

To undo everything from this step:

```sql
DROP DATABASE iot_curated;
DROP USER 'eaip_readonly'@'localhost';
```

---

## Step 2 — get the code onto the server

```bash
mkdir -p /opt/eaip
cd /opt/eaip
git clone <your-repo-url> .
```

Deliberately **not** under `/www/wwwroot/`. That directory is aaPanel's, and
aaPanel manages what it finds there — a site entry, an Nginx vhost, possibly a
permissions sweep. `/opt` is the conventional place for software that is not a
website, and it keeps EAIP's own files outside anything aaPanel touches. The
aaPanel *site* created in Step 5 still points at this directory's built output
— aaPanel manages the process and the proxy, not the source tree.

---

## Step 3 — backend configuration

```bash
cd /opt/eaip/backend
cp ../.env.example .env
nano .env
```

Set these, and generate the secrets rather than inventing them:

```bash
# Run these and paste the output into .env:
openssl rand -hex 32                      # JWT_SECRET
python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
                                          # CREDENTIAL_ENCRYPTION_KEY
```

| Variable | Value |
|---|---|
| `APP_ENV` | `production` |
| `POSTGRES_PASSWORD` | a fresh strong password |
| `POSTGRES_READONLY_PASSWORD` | a different fresh password |
| `JWT_SECRET` | from `openssl rand -hex 32` — kept only so the test-only issuer has a value; see the note below |
| `CREDENTIAL_ENCRYPTION_KEY` | from the python line above |
| `POSTGRES_PORT` | `5433` |
| `REDIS_PORT` | `6380` |
| `CORS_ORIGINS` | `https://aiplatform.clbgroups.com` |
| `OIDC_JWKS_URL` | `https://aiplatform.clbgroups.com/realms/eaip/protocol/openid-connect/certs` |
| `OIDC_ISSUER` | `https://aiplatform.clbgroups.com/realms/eaip` |
| `EAIP_DOMAIN` | `aiplatform.clbgroups.com` (no scheme — read by `docker-compose.prod.yml`) |
| `KEYCLOAK_ADMIN` | a real admin username, not `admin` |
| `KEYCLOAK_ADMIN_PASSWORD` | a fresh strong password |
| `KEYCLOAK_DB_PASSWORD` | a fresh strong password, different from the others |

Setting `OIDC_JWKS_URL`/`OIDC_ISSUER` is what switches the backend to RS256
verification against Keycloak's real public keys — `JWT_SECRET`'s local issuer
stays present only because the test suite needs a value, and it refuses to run
in this configuration anyway (`APP_ENV=production` plus a provider set — see
CLAUDE.md's Phase 11 section).

```bash
chmod 600 .env
```

`.env` is gitignored and must stay that way. The IoT password from Step 1 goes
into the connector through the console at Step 8, **not** into this file —
connector credentials are encrypted with `CREDENTIAL_ENCRYPTION_KEY` and stored
in the database, and no endpoint returns one.

---

## Step 4 — frontend configuration

```bash
cd /opt/eaip/frontend
cp .env.example .env.local
nano .env.local
```

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_API_URL` | `https://aiplatform.clbgroups.com` |
| `NEXT_PUBLIC_OIDC_ISSUER` | `https://aiplatform.clbgroups.com/realms/eaip` |
| `NEXT_PUBLIC_OIDC_CLIENT_ID` | `eaip-console` |

These are `NEXT_PUBLIC_*` — compiled into the browser bundle at **build time**,
not read at runtime. Changing one later means rebuilding (`npm run build`),
not just restarting the process. `NEXT_PUBLIC_API_URL` has **no path suffix**
— `frontend/src/lib/api.ts` appends `/v1/...` itself on every call, so the
value here is just the origin, matching the `/v1` proxy rule in Step 6 rather
than inventing a prefix the backend doesn't have.

---

## Step 5 — start the Docker-based services

```bash
cd /opt/eaip
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d \
  postgres redis keycloak keycloak-db
```

Not `up -d` alone — that would also start the `mysql` test-fixture service if
its profile were ever left active by mistake. Naming the four production
services explicitly is the safer form on a shared host.

**Why loopback binding rather than a firewall rule.** Docker writes its own
iptables rules, ahead of UFW and ahead of what aaPanel's firewall page shows. A
port published as `5433:5432` is reachable from the internet even when the
panel says the port is closed. `127.0.0.1:5433:5432` is reachable only from the
host itself. The binding is the control; the firewall is not. Verify:

```bash
docker compose ps                              # all four healthy
ss -lntp | grep -E ':(5433|6380|8081)\b'       # must show 127.0.0.1, NOT 0.0.0.0
```

If any line shows `0.0.0.0`, the overlay was not applied — stop and re-run
with both `-f` flags.

**Keycloak takes longer than Postgres or Redis to report healthy** — it is
running its own database migrations and importing the realm on this first
boot. Watch it directly rather than assuming `docker compose ps` will settle
quickly:

```bash
docker compose logs -f keycloak
# wait for: "Keycloak ... started ..." and "Profile prod activated."
```

Confirm the realm actually imported and the hostname is correct:

```bash
curl -s http://127.0.0.1:8081/realms/eaip/.well-known/openid-configuration \
  | grep -o '"issuer":"[^"]*"'
# must read: "issuer":"https://aiplatform.clbgroups.com/realms/eaip"
```

If the issuer instead shows `http://127.0.0.1:8081/realms/eaip` or similar,
`EAIP_DOMAIN` was not set (or not exported) before `up -d` ran — fix `.env`
and `docker compose up -d keycloak` again to re-read it.

Confirm nothing else on the host moved:

```bash
systemctl status mariadb --no-pager | head -3
curl -sI https://iotplatform.clbgroups.com | head -1
```

---

## Step 6 — aaPanel: the site and its reverse proxy rules

In the aaPanel GUI:

**Website → Add site**
- Domain: `aiplatform.clbgroups.com`
- Leave the PHP version as "Pure static" or "None" — nothing here is served by
  PHP-FPM. The site entry exists so aaPanel has somewhere to hang the reverse
  proxy rules and the SSL certificate; its document root is never used.

**SSL tab → Let's Encrypt → apply for the domain**, then turn "Force HTTPS" on.
This is the certificate for the one public domain; Keycloak, the backend, and
the frontend never see it directly — they all speak plain HTTP on loopback,
and aaPanel's Nginx is the only thing terminating TLS. That's why
`KC_HTTP_ENABLED=true` is set in `docker-compose.prod.yml` rather than trying
to give Keycloak its own certificate.

**Reverse Proxy tab → Add reverse proxy**, one rule per row below. aaPanel's
reverse-proxy feature is path-scoped per rule — add each as its own entry
rather than trying to express all of them as one:

| Proxy name | Target path | Target URL |
|---|---|---|
| eaip-api | `/v1` | `http://127.0.0.1:8000` |
| eaip-health | `/health` | `http://127.0.0.1:8000` |
| eaip-keycloak-realms | `/realms` | `http://127.0.0.1:8081` |
| eaip-keycloak-admin | `/admin` | `http://127.0.0.1:8081` |
| eaip-keycloak-resources | `/resources` | `http://127.0.0.1:8081` |
| eaip-frontend | `/` | `http://127.0.0.1:3000` |

Each rule must forward the path **unchanged** (no prefix stripping) — aaPanel's
reverse proxy GUI typically preserves the matched path by default, but check
whichever "send path as-is" / rewrite option it exposes rather than assuming.

**Order matters — put `/` last.** aaPanel matches proxy rules in the order
they're listed; if the catch-all `/` rule is evaluated first it swallows every
other path before Keycloak's or the backend's more specific rule gets a
chance. If the GUI does not expose a way to reorder them, delete and re-add
in the order above.

**Verify each path independently before moving on:**

```bash
curl -sI https://aiplatform.clbgroups.com/                                  # frontend, 200
curl -s  https://aiplatform.clbgroups.com/health                            # backend, db/redis: ok
curl -s  https://aiplatform.clbgroups.com/realms/eaip/.well-known/openid-configuration \
  | grep -o '"issuer":"[^"]*"'                                              # must match the public URL exactly
```

A working `curl` from the server itself and a broken browser login is the
classic failure mode here — it means one of the three rules didn't take, or
matched in the wrong order. Test from a browser too, not just curl, once all
three succeed.

---

## Step 7 — the backend and frontend, as aaPanel-managed processes

Not a bare terminal session, and not Docker — the backend and frontend are
ordinary long-running processes, kept alive and restarted by aaPanel's own
process managers, the same way aaPanel already manages every other site on
this host.

**Backend — aaPanel's Python Project Manager:**
1. Install `uv` on the host if not already present (aaPanel's Python plugin
   manages interpreters, not `uv` itself):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
2. Website → Python Project → Add project.
   - Project directory: `/opt/eaip/backend`
   - Startup file / command: run `uv sync` once by hand first
     (`cd /opt/eaip/backend && uv sync`), then set the project's startup
     command to `uv run uvicorn app.main:app --host 127.0.0.1 --port 8000`.
   - Port: `8000`, bound to `127.0.0.1` — matches the reverse proxy target in
     Step 6, and never published beyond loopback for the same reason the
     Docker services aren't.
   - Auto-restart: on.
3. Before first start, run migrations once (not something the process
   manager's startup command should do on every restart):
   ```bash
   cd /opt/eaip/backend
   uv run alembic upgrade head
   ```

**Frontend — aaPanel's Node.js (PM2) manager:**
1. Install dependencies and build once by hand:
   ```bash
   cd /opt/eaip/frontend
   npm install
   npm run build
   ```
2. Node.js project manager → Add project.
   - Project directory: `/opt/eaip/frontend`
   - Startup command: `npm run start`
   - Port: `3000`, bound to `127.0.0.1`.
   - Auto-restart: on.

**Rebuilding after a config or code change:** a code change to the backend
just needs the process restarted (aaPanel's manager does this from the GUI). A
frontend code change, or any change to a `NEXT_PUBLIC_*` variable, needs
`npm run build` run again before restarting — `next start` serves whatever the
last build produced, not the source tree live.

---

## Step 8 — the first admin user

Keycloak's realm import creates no users (`infra/keycloak/README.md` — a
realm that creates a working account creates a default credential). Create the
first one by hand, in the admin console, now reachable at
`https://aiplatform.clbgroups.com/admin`:

realm **eaip** → Users → Add user, following the same steps as local
development — username, email (verified on), the **EAIP tenant id** and
**EAIP permission labels** attributes, a non-temporary password, and the
`admin` realm role. The tenant must already exist in EAIP's database; list
them with:

```bash
docker exec -it eaip-postgres psql -U $POSTGRES_USER -d $POSTGRES_DB -c "SELECT id, slug FROM tenant;"
```

Log in at `https://aiplatform.clbgroups.com` to confirm before going further.

---

## Step 9 — the IoT connector

In the console, add an integration:

| Field | Value |
|---|---|
| Kind | `sql` |
| Engine | `mysql` — correct for MariaDB too; the connector handles both |
| Host | `127.0.0.1` |
| Port | `3306` |
| Database | `iot_curated` |
| Username | `eaip_readonly` |
| Password | the one from Step 1 |
| Schema | `iot_curated` |
| Allow private / loopback | on — the database is on this host |

Host `127.0.0.1` and not the public IP: the connection stays inside the machine
and never touches the network.

---

## Reaching things directly, for debugging

The public path is the browser at `https://aiplatform.clbgroups.com`. An SSH
tunnel is still the way to reach a loopback-bound service directly, bypassing
the reverse proxy, when narrowing down whether a problem is in aaPanel's Nginx
or in EAIP itself:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 -L 8081:127.0.0.1:8081 root@<host>
```

then compare `http://127.0.0.1:8000/health` (direct) against
`https://aiplatform.clbgroups.com/health` (through the proxy) — a
difference between those two localizes the problem immediately.

---

## Rolling back

```bash
cd /opt/eaip
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
```

Containers stop; volumes and data remain. Then, in aaPanel: stop the Python
and Node.js projects from Step 7, and remove or disable the site's reverse
proxy rules if the domain needs to point elsewhere.

To remove everything EAIP created:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down -v
rm -rf /opt/eaip
mysql -uroot -p -e "DROP DATABASE iot_curated; DROP USER 'eaip_readonly'@'localhost';"
```

`down -v` also deletes Keycloak's own database volume — every user, role
assignment, and client secret created after the initial realm import. That is
a second, separate loss from the EAIP data in Postgres; make sure a Keycloak
backup exists first if the rollback isn't a full teardown (see below).

Nothing in this sequence touches an existing site, database, or user.

---

## What this still does NOT establish

Being explicit, so nobody over-reads a working deployment:

- **Daily backups exist and are restore-verified, but only in isolation.**
  Both EAIP's own Postgres and Keycloak's database (a separate backup
  obligation — a restore that recovers EAIP but not Keycloak recovers a
  platform nobody can log into) are dumped daily via cron and each has
  actually been restored into a disposable container and queried, not just
  assumed to work. What hasn't been drilled is restoring the *whole stack
  together* on this host after a real loss — the individual pieces are
  proven, the end-to-end recovery procedure is not.
- **No monitoring or alerting.** If a process crashes, aaPanel's process
  manager restarts it — but nothing pages anyone, and nothing tracks whether
  it's crash-looping.
- **No load test.** This has been proven to boot and to route correctly, not
  proven under real concurrent use.
- **MES and WMS must not be connected to it.** IoT only, until this has run
  long enough to trust with more.
- **It cannot compute OEE.** `oee_device_config` is populated for 97 devices,
  but the metrics that feed OEE — `total_count`, `reject_count` — have 92 rows
  between them and stopped on 2026-06-19. That is a data-collection gap in the
  IoT platform, not something EAIP can close.

## Two things worth raising with whoever owns that host

Neither is caused by this deployment; both were visible while preparing it.

- **No database backups.** aaPanel shows auto-backup off and every database
  reading `Backup: Not exist` — including `dms_main`, `wms`, `evolve_main`, and
  `rongmah_main`. A disk failure loses all of it. EAIP's own Postgres and
  Keycloak's database now add two more things this applies to.
- **MariaDB listens on `*:3306`**, so it is reachable from the internet. The
  ~11,000 failed logins in the error log are all `@'localhost'` — a stale
  application password retrying, not an intrusion — but the exposure is real
  regardless of whether anyone has used it yet.
