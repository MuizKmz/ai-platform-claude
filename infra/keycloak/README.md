# Keycloak — the identity provider

Why Keycloak and not something else is [ADR 0009](../../docs/adr/0009-keycloak-for-identity.md).
This file is how to run it, and why the realm is shaped the way it is.

`realm-eaip.json` carries no comments because **Keycloak refuses an import
containing unknown fields** — a `_comment` key fails the whole realm with
`Unrecognized field`, and the container then restart-loops. That is why the
reasoning lives here instead.

## Starting it

```powershell
docker compose up -d keycloak-db keycloak
```

First start takes about 60–90 seconds: it initialises its database, runs its own
migrations, then imports the realm. `docker compose ps` should show both healthy.

- Admin console: <http://localhost:8081/admin> (`admin` / `admin` by default)
- Realm issuer: <http://localhost:8081/realms/eaip>
- JWKS: <http://localhost:8081/realms/eaip/protocol/openid-connect/certs>

**Port 8081, not 8080.** 8080 is the most contested port on a development
machine, and this project already publishes non-default ports on purpose
(Postgres 5433, Redis 6380, MySQL 3307).

## The import runs once

`--import-realm` imports only when the realm does not already exist. After the
first start, **the running Keycloak is the source of truth** and editing the
JSON changes nothing. To pick up a change to this file:

```powershell
docker compose down keycloak
docker volume rm sourcecode_keycloakdata   # DESTROYS users, sessions, everything
docker compose up -d keycloak-db keycloak
```

That is deliberate. A realm that silently reverted to a file on every restart
would discard the users and role assignments an operator made in the console.

## Why the realm looks like this

**No users are defined.** A realm import that creates a working account creates
a default credential, and default credentials outlive the development they were
meant for. Create the first one yourself — see below.

**`accessTokenLifespan` is 300 seconds.** Short tokens are what make revocation
meaningful: disabling a user ends their access within five minutes rather than
whenever their token happens to expire. The old CLI tokens lasted 60 minutes and
could not be revoked at all.

**Brute-force protection is on.** Five failures and the account locks for a
growing interval. The CLI issuer had no such concept because it checked no
credentials — there was nothing to guess.

**`eaip-console` is a public client with PKCE.** A single-page app cannot keep a
secret: anything shipped to a browser is readable by whoever is using it. PKCE
secures the flow instead of a client secret that would be a secret in name only.

**`eaip-mcp` is a separate client with its own audience.** RFC 8707, and
[ADR 0007](../../docs/adr/0007-mcp-implemented-directly-rather-than-with-the-sdk.md):
a console token must not work as a machine credential on the MCP surface. The
API has always *checked* the audience; this is where the separation is issued.

**Claims are namespaced URLs** (`https://eaip.dev/tenant_id`), not bare names, so
a claim Keycloak adds in a future version cannot collide with one of ours and
quietly change its meaning. Note the escaped dot — `https://eaip\\.dev/...` in
the JSON. Keycloak treats the claim name as a dotted path, so an unescaped dot
would nest the claim inside an object named `eaip` instead of producing the flat
claim the API reads.

### Two things that cost an afternoon

Both were found by running the import, not by reading it. `test_keycloak_realm.py`
now pins them.

**Declaring `clientScopes` REPLACES Keycloak's built-in set.** It does not add to
it. The moment this realm defined `eaip-identity`, the standard `basic`, `email`,
`profile`, and `roles` scopes stopped existing — so a token came back with no
`email`, no roles, and **no `sub`**.

`sub` is the damaging one. `app/core/oidc.py` requires it and derives `user_id`
from it, so its absence fails every login with *"token is missing required
identity claims"* — which reads like a bad password and is not. That is why this
realm carries its own `eaip-subject`, `eaip-email`, and `eaip-realm-roles`
mappers instead of referencing built-ins the import removed.

**`userProfile` is not a realm key.** Keycloak stores it as a *component* whose
config is the profile serialised as a JSON string:

```json
"components": {
  "org.keycloak.userprofile.UserProfileProvider": [
    { "providerId": "declarative-user-profile",
      "config": { "kc.user.profile.config": ["{...the profile, as a string...}"] } }
  ]
}
```

An inline `"userProfile": {...}` is rejected with `unable to read contents from
stream`, which names neither the field nor the reason.

### The mappers are load-bearing

`eaip-tenant-id` is the one that matters. Without it a token verifies perfectly
and carries no tenant — and the API **refuses** it rather than defaulting,
because "no tenant" must never come to mean "all tenants" (security invariant
#1). If every login starts failing with *"token is missing required identity
claims"*, check this mapper before anything else.

Each user needs two attributes set, which is what those mappers read. Because
the realm declares them in its user profile, they appear as ordinary fields on
the user form rather than under a separate Attributes tab — and Keycloak 26
**silently discards** any attribute it has not been told about, so a realm
missing that declaration drops `tenant_id` without an error:

| Attribute | Example | Meaning |
|---|---|---|
| `tenant_id` | `a3f1…` (UUID) | Which tenant's data they may see. Must match a row in EAIP's `tenant` table. |
| `labels` | `public`, `iot` | The permission labels retrieval filters on. |

Roles (`reader`, `analyst`, `admin`) are realm roles, assigned in the Role
Mapping tab.

## Creating the first admin user

In the admin console, realm **eaip** → Users → Add user:

1. Username and email; **Email verified** on (there is no mail server here).
2. Fill in **EAIP tenant id** and **EAIP permission labels** on the same form —
   they are declared attributes, so they are fields, not a separate tab.
3. Credentials tab → Set password, **Temporary off**.
4. Role mapping tab → assign `admin`.

The `tenant_id` must be a tenant that exists in EAIP's database. List them:

```powershell
docker exec -it eaip-postgres psql -U $env:POSTGRES_USER -d $env:POSTGRES_DB -c "SELECT id, slug FROM tenant;"
```

## Letting an external AI use EAIP over MCP

A separate identity, a separate audience, and a separate document:
[MCP-SETUP.md](MCP-SETUP.md). The short version — `eaip-mcp`'s service account
needs the same `tenant_id`/`labels` treatment as a user, set on its
service-account user rather than through a login it never performs, and its
tokens carry `eaip-api-mcp` as the audience, not `eaip-api`.

## Creating test accounts for the E2E suite

`frontend/e2e/console.spec.ts` drives real logins, and once this realm has an
identity provider configured the console shows Keycloak's redirect rather
than the paste-a-token form (`frontend/e2e/helpers.ts`'s `signInViaProvider`).
Two fixture accounts back that: `OIDC_ADMIN` and `OIDC_READER`, defined in
`helpers.ts` with their usernames and emails as literals — they identify
fixtures in a realm meant for this, not secrets — and their passwords read
from `E2E_OIDC_ADMIN_PASSWORD` / `E2E_OIDC_READER_PASSWORD`.

To (re)create them: Users → Add user, same fields as "Creating the first
admin user" above.

| | `eaip-e2e-admin2` | `eaip-e2e-reader` |
|---|---|---|
| Email | `eaip-e2e-admin2@acme.test` | `eaip-e2e-reader@acme.test` |
| Tenant | whichever tenant `acme`'s slug resolves to | same |
| Labels | `public`, `finance`, `iot` | `public` |
| Role | `admin` | `reader` |

**The `2` is not a typo, and worth understanding before renaming it.** A
first `eaip-e2e-admin` account failed every password login with
`invalid_user_credentials` for a reason that did not reproduce on a fresh
account with identical settings — role, labels, and a hex-only password all
individually ruled out as the cause. Deleting it and creating
`eaip-e2e-admin2` fresh fixed it immediately. If this happens again: a
throwaway credential is not worth extended diagnosis, and delete-and-recreate
is the faster, equally valid fix. (If you rename it back to `eaip-e2e-admin`
successfully, update `OIDC_ADMIN.username` in `helpers.ts` to match — a
Keycloak `PUT` on this user's own record failed with 400 when tried
immediately after creation, for reasons not investigated.)

**One test needs a matching `app_user` row.** `test("an admin cannot delete
their own account")` depends on the API's own rule — it compares by
`principal.user_id`, the token's `sub`, not by email — so "your own row" only
exists if an `app_user` row shares that exact `id`. Find the Keycloak user's
`id` (Users → the account → its UUID in the URL) and insert it directly:

```sql
INSERT INTO app_user (id, tenant_id, email, roles, allowed_labels)
VALUES ('<keycloak user id>', '<acme tenant id>', 'eaip-e2e-admin2@acme.test',
        ARRAY['admin','reader'], ARRAY['public','finance','iot']);
```

This is the visible edge of a real gap: EAIP's `app_user` table and
Keycloak's user directory are two separate stores with no sync between them.
A person can exist in one and not the other — your own console login almost
certainly has no `app_user` row today, which is why the Users page not
listing you is expected, not a bug.

Running the suite:

```powershell
$env:E2E_OIDC_ADMIN_PASSWORD = "..."
$env:E2E_OIDC_READER_PASSWORD = "..."
cd frontend
npm run e2e
```

## Pointing EAIP at it

In `.env`:

```
OIDC_JWKS_URL=http://localhost:8081/realms/eaip/protocol/openid-connect/certs
OIDC_ISSUER=http://localhost:8081/realms/eaip
```

Setting these switches token verification to RS256 against Keycloak's public
keys. The API can then verify an identity and **can no longer mint one** — which
is the entire point of ADR 0009.

Leave them empty and the built-in development issuer stays in use. That issuer
performs no credential check, so it refuses to run when `APP_ENV=production` and
a provider is configured.

## Verifying it actually works

```powershell
# The realm is up and publishing keys:
curl.exe -s http://localhost:8081/realms/eaip/.well-known/openid-configuration

# The claim mappers fire — decode the access token and look for tenant_id.
# Requires a user created as above.
```

A token that verifies but produces `token is missing required identity claims`
means the mappers are wrong, not the password.

## Production differences

`docker-compose.yml` runs `start-dev`, which serves plain HTTP and relaxes
hostname checks. **It is not a production command.** Production needs `start`
behind TLS with `KC_HOSTNAME` set, and the bootstrap admin variables removed so
there is no default credential. That is the TLS/deployment part of Phase 11 and
is not done yet.

Keycloak's database is a **separate backup obligation** from EAIP's. A restore
that recovers EAIP but not Keycloak recovers a platform nobody can log into.
