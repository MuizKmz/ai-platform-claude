# Deploying to the aaPanel host

> **STATUS: NOT PERFORMED.** This is a plan, not a record. EAIP has never run
> on that host.
>
> What actually exists today: EAIP runs on a **developer's machine** — backend,
> frontend, Postgres, Redis, and Keycloak in local Docker — and reaches the IoT
> MariaDB on the Huawei host over an **SSH tunnel**. The remote database is
> read; the platform is not deployed there.
>
> The distinction matters. Read in the present tense this document describes a
> deployment that does not exist, and decisions get made against it — the port
> 80/443 conflict below is real *if* you deploy here, and irrelevant until then.
>
> It is also **out of date in one respect**: it predates Phase 11. Identity is
> now Keycloak (ADR 0009), which adds two containers, its own database, its own
> backup obligation, and a hostname requirement that plain `start-dev` does not
> satisfy. Steps 4 and 5 do not account for any of that.

The target is the Huawei Cloud VM that already runs the IoT platform, a WMS,
and around a dozen client databases under aaPanel.

**The governing constraint is that none of those may be disturbed.** Everything
below follows from it. If a step here would affect an existing service, that is
a bug in this document, not an acceptable cost.

This is a **pilot**, not a production rollout: EAIP deployed on production
hardware, pointed at one non-production data source, used by one person. Worth
being precise about the wording — "we tested in production" invites a no from
whoever approves this; "a read-only pilot against IoT mock data on existing
hardware" describes the same thing and is defensible.

---

## What this does and does not touch

| Touched | Not touched |
|---|---|
| A new directory under `/opt/eaip` | `/www/wwwroot/*` — every existing site |
| Three new Docker containers | The host's MariaDB service |
| Host ports 5433, 6380, 3000, 8000 — **loopback only** | Ports 80, 443, 3306, and aaPanel's own |
| A new MariaDB database `iot_curated` (views) | `iot_db` and every other database |
| A new MariaDB user `eaip_readonly` | Every existing MariaDB user |

Nothing in an existing site's document root is read, written, or moved.

---

## Before you start

**1. The IoT team should know.** A read-only user plus a views database on their
production server is a small change, but it is their server. Get the nod.

**2. Check there is room.**

```bash
free -m          # need ~1.5 GB free; the containers are capped at ~1.8 GB total
df -h /          # need ~5 GB
docker --version # if this fails, see "Installing Docker" below
```

If `free -m` shows under 1.5 GB available, stop and tell me — the limits in
`docker-compose.prod.yml` need lowering, and that is a deliberate decision
rather than something to squeeze.

**3. Confirm the host's ports are free.** These are the ones EAIP wants:

```bash
ss -lntp | grep -E ':(3000|8000|5433|6380)\b'
```

Empty output is what you want. Anything listed means a port collision, and the
fix is to change EAIP's port in `.env` — never to stop whatever is already
there.

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
#   keep that password; it goes in .env at step 3
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
website, and it keeps EAIP outside anything aaPanel touches.

---

## Step 3 — configuration

```bash
cd /opt/eaip
cp .env.example .env
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
| `JWT_SECRET` | from `openssl rand -hex 32` |
| `CREDENTIAL_ENCRYPTION_KEY` | from the python line above |
| `POSTGRES_PORT` | `5433` |
| `REDIS_PORT` | `6380` |
| `CORS_ORIGINS` | `http://127.0.0.1:3000` |

```bash
chmod 600 .env
```

`.env` is gitignored and must stay that way. The IoT password from step 1 goes
into the connector through the console at step 6, **not** into this file —
connector credentials are encrypted with `CREDENTIAL_ENCRYPTION_KEY` and stored
in the database, and no endpoint returns one.

---

## Step 4 — start it

```bash
cd /opt/eaip
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Both files, in that order. The overlay is what binds ports to loopback, caps
memory, and leaves the bundled MySQL out — it is a test fixture, and production
has no use for it.

**Why loopback binding rather than a firewall rule.** Docker writes its own
iptables rules, ahead of UFW and ahead of what aaPanel's firewall page shows. A
port published as `5433:5432` is reachable from the internet even when the
panel says the port is closed. `127.0.0.1:5433:5432` is reachable only from the
host itself. The binding is the control; the firewall is not.

Verify:

```bash
docker compose ps                       # postgres and redis, both healthy
ss -lntp | grep -E ':(5433|6380)\b'     # must show 127.0.0.1, NOT 0.0.0.0
```

If either line shows `0.0.0.0`, the overlay was not applied — stop and re-run
with both `-f` flags.

Confirm nothing else moved:

```bash
systemctl status mariadb --no-pager | head -3
curl -sI https://iotplatform.clbgroups.com | head -1
```

---

## Step 5 — migrations and a first user

```bash
cd /opt/eaip/backend
docker run --rm --network host --env-file ../.env -v "$PWD:/app" -w /app \
  python:3.12-slim bash -c "pip install -q uv && uv run alembic upgrade head"
```

Then create a tenant and an admin as described in `docs/RUNBOOK.md`.

---

## Step 6 — the IoT connector

In the console (reached by tunnel, see below), add an integration:

| Field | Value |
|---|---|
| Kind | `sql` |
| Engine | `mysql` — correct for MariaDB too; the connector handles both |
| Host | `127.0.0.1` |
| Port | `3306` |
| Database | `iot_curated` |
| Username | `eaip_readonly` |
| Password | the one from step 1 |
| Schema | `iot_curated` |
| Allow private / loopback | on — the database is on this host |

Host `127.0.0.1` and not the public IP: the connection stays inside the machine
and never touches the network.

---

## Reaching the console

Nothing is published publicly, so from your laptop:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 root@<host>
```

Leave that running, then open <http://127.0.0.1:3000>.

The tunnel rides an SSH session that is already encrypted and authenticated,
which is why a pilot needs no TLS certificate. Serving this to other people
does — that is Phase 11, along with a real identity provider.

---

## Rolling back

```bash
cd /opt/eaip
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
```

Containers stop; volumes and data remain. To remove everything EAIP created:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down -v
rm -rf /opt/eaip
mysql -uroot -p -e "DROP DATABASE iot_curated; DROP USER 'eaip_readonly'@'localhost';"
```

Nothing in that sequence touches an existing site, database, or user.

---

## What this pilot does NOT establish

Being explicit, so nobody over-reads a working demo:

- **It is not multi-user.** ~~Tokens are issued by hand.~~ **Superseded:**
  identity is now Keycloak (ADR 0009), with real passwords, lockout, and
  revocation. That unblocks multi-user — but this document has not been updated
  to deploy it, and Keycloak here would need `start` behind TLS with
  `KC_HOSTNAME` set, not the `start-dev` the base compose file uses.
- **It has no TLS.** The SSH tunnel is the encryption. Serving anyone else
  needs a certificate — and on this host ports 80 and 443 belong to aaPanel's
  Nginx, so a Caddy container cannot simply take them. The likely shape is
  aaPanel's own reverse proxy and Let's Encrypt for a subdomain, forwarding to
  EAIP's loopback ports. Not designed, not tested.
- **MES and WMS must not be connected to it.** IoT only, mock data only, until
  identity is real.
- **It cannot compute OEE.** `oee_device_config` is populated for 97 devices,
  but the metrics that feed OEE — `total_count`, `reject_count` — have 92 rows
  between them and stopped on 2026-06-19. That is a data-collection gap in the
  IoT platform, not something EAIP can close.

## Two things worth raising with whoever owns that host

Neither is caused by this deployment; both were visible while preparing it.

- **No database backups.** aaPanel shows auto-backup off and every database
  reading `Backup: Not exist` — including `dms_main`, `wms`, `evolve_main`, and
  `rongmah_main`. A disk failure loses all of it.
- **MariaDB listens on `*:3306`**, so it is reachable from the internet. The
  ~11,000 failed logins in the error log are all `@'localhost'` — a stale
  application password retrying, not an intrusion — but the exposure is real
  regardless of whether anyone has used it yet.
