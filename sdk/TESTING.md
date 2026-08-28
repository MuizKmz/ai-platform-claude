# Testing and first use of the chat widget SDK

The step-by-step for going from "the packages are built" to "I have watched
the real widget answer a real question against real EAIP." Written to be
picked up and worked through later; each step says who does it and how to
know it worked.

Two related docs:
- [SETUP.md](./SETUP.md) — the tutorial an *integrating developer* follows.
- [../docs/CHAT_WIDGET_SDK.md](../docs/CHAT_WIDGET_SDK.md) — the design and why.

This file is the *first* time through, done by us, locally, to prove the chain.

---

## Where things stand

| Piece | State |
|---|---|
| `sdk/eaip-client`, `sdk/eaip-widget`, `sdk/eaip-proxy-endpoint` | **Built. 44 tests green against fakes.** |
| Run against real Keycloak + real EAIP `/mcp` | **Not done — this doc.** |
| A demo host app that mounts the widget | **Not built — step 5 below.** |
| IoT integration migrated onto the widget | Not started (a later Phase 12 item) |

Nothing here needs a code change to the SDK. What's missing is configuration
only you / an EAIP admin can supply, plus a small throwaway demo app.

---

## The blockers, up front

1. **`eaip-mcp` has no client secret set** — you generate it in Keycloak.
2. **`eaip-mcp`'s service account has no `tenant_id` / `labels`** — you set
   them in Keycloak. This is deliberate: it's an access decision, kept out of
   the realm file the same way the first admin user is (see
   [../infra/keycloak/MCP-SETUP.md](../infra/keycloak/MCP-SETUP.md)).
3. **The EAIP backend isn't running locally** — one command.
4. **`EAIP_MCP_URL` for a real integrator** — in production this is just
   `https://aiplatform.clbgroups.com/mcp` (publicly routed, confirmed). For
   *this local test* it's `http://127.0.0.1:8000/mcp`.

---

## Step 1 — Bring the stack up

```powershell
# from repo root
docker compose up -d          # Postgres, Redis, MySQL, Keycloak
cd backend
uv run uvicorn app.main:app --reload
```

**Verify:**
```powershell
curl.exe http://127.0.0.1:8000/health
# -> {"status":"ok","db":"ok","redis":"ok"}

curl.exe http://127.0.0.1:8081/realms/eaip/.well-known/openid-configuration
# -> JSON with "issuer":"http://127.0.0.1:8081/realms/eaip"
```

If `.env` in `backend/` has `OIDC_JWKS_URL` / `OIDC_ISSUER` set, the backend
verifies tokens against Keycloak (what we want here). If they're empty, it
uses the local dev issuer and the MCP path won't accept a Keycloak token —
set them to the local Keycloak:

```
OIDC_JWKS_URL=http://localhost:8081/realms/eaip/protocol/openid-connect/certs
OIDC_ISSUER=http://localhost:8081/realms/eaip
```

---

## Step 2 — Give `eaip-mcp` a secret and an identity  *(you, in Keycloak)*

Open <http://localhost:8081/admin> (default `admin` / `admin`), realm **eaip**.

### 2a. The secret

**Clients → `eaip-mcp` → Credentials tab → Client secret → Copy.**
(If it's blank, click **Regenerate**.) Keep it for step 3 — treat it like a
password, don't paste it into a commit or a ticket.

### 2b. The identity

**Users → `service-account-eaip-mcp` → Details tab.** Set:

| Field | Value |
|---|---|
| Email | `service-account-eaip-mcp@eaip.local` |
| Email verified | **ON** |
| EAIP tenant id | `a31176dd-6479-4bf9-ad97-027c0b468b0a` *(the `acme` tenant — it has documents)* |
| EAIP permission labels | `public` |

**Save.**

> Why these exact values: `acme` is the local tenant with real `public`
> documents (a handbook, a parts catalogue, a data-retention policy). A
> service account with no `tenant_id` is **refused** by the API, not
> defaulted — invariant #1.

To re-check the tenant list yourself:
```powershell
docker exec -it eaip-postgres psql -U eaip -d eaip -c "SELECT id, slug FROM tenant;"
```

---

## Step 3 — Prove the MCP chain with raw curl  *(before the SDK)*

This is the SDK's job reduced to three HTTP calls. If this works, the SDK is
just a nicer wrapper.

```bash
SECRET="<the secret from step 2a>"

# 3a. Get a token
TOKEN=$(curl -s -X POST http://localhost:8081/realms/eaip/protocol/openid-connect/token \
  -d "client_id=eaip-mcp" -d "client_secret=$SECRET" -d "grant_type=client_credentials" \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

echo "$TOKEN" | cut -c1-20   # sanity: non-empty

# 3b. Decode it — confirm audience and claims
echo "$TOKEN" | python -c "
import sys,base64,json
p = sys.stdin.read().split('.')[1]
print(json.dumps(json.loads(base64.urlsafe_b64decode(p + '==')), indent=2))
"
# want: "aud" contains "eaip-api-mcp"  (NOT just "eaip-api")
#       "https://eaip.dev/tenant_id"  == the acme UUID
#       "https://eaip.dev/labels"     includes "public"

# 3c. List tools
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | python -m json.tool
# want: a "result.tools" array containing at least "search_knowledge"

# 3d. Ask a real question
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search_knowledge","arguments":{"query":"what is the data retention policy"}}}' \
  | python -m json.tool
# want: "result.content[0].text" is a grounded answer, "result.isError" false
```

**Failure guide:**
- `token endpoint` 401 → wrong secret, or the client is disabled.
- `/mcp` returns `{"error":{"code":-32001,"message":"Unauthorized"}}` → the
  token's audience is `eaip-api`, not `eaip-api-mcp` (check the `eaip-mcp`
  client's **Client scopes** tab has `eaip-mcp-audience`), OR the backend
  isn't verifying against Keycloak (step 1's `.env` note).
- `tools/list` returns only `search_knowledge` and you expected `query_*`
  tools → the service account's labels don't include the label those
  connectors require. Add it in step 2b.
- `"token is missing required identity claims"` → the service-account user
  has no `tenant_id`, or Email verified is off. Step 2b.

---

## Step 4 — Point the proxy endpoint at local EAIP  *(us)*

A throwaway `.env` for the demo app (step 5). **Not** committed.

```
EAIP_MCP_TOKEN_URL=http://localhost:8081/realms/eaip/protocol/openid-connect/token
EAIP_MCP_CLIENT_ID=eaip-mcp
EAIP_MCP_CLIENT_SECRET=<the secret from step 2a>
EAIP_MCP_URL=http://127.0.0.1:8000/mcp
```

---

## Step 5 — A minimal demo host app  *(us — scratchpad, not the repo)*

The smallest thing that exercises the real widget:

```
demo-host/
  server.js        # Express: mounts eaipProxyRouter() at /api/eaip, serves the page
  index.html       # loads React + the built widget, renders <EaipChat basePath="/api/eaip" />
  .env             # the four vars from step 4
```

`server.js` is essentially:

```js
import express from "express";
import { eaipProxyRouter } from "@eaip/proxy-endpoint/express";

const app = express();
app.use("/api/eaip", eaipProxyRouter());   // reads .env
app.use(express.static("."));              // serves index.html
app.listen(4000, () => console.log("demo host on http://localhost:4000"));
```

Because the three SDK packages aren't published to npm, the demo installs
them from the local paths:

```bash
cd demo-host
npm init -y
npm install express react react-dom
npm install ../../sdk/eaip-client ../../sdk/eaip-proxy-endpoint ../../sdk/eaip-widget
# build eaip-client first (eaip-widget imports its dist/)
( cd ../../sdk/eaip-client && npm run build )
( cd ../../sdk/eaip-widget && npm run build )
```

**Verify, in order:**

1. `curl -X POST http://localhost:4000/api/eaip/session` → `{"ok":true}`
   - `503` → the `.env` is wrong or the secret is bad (same causes as 3a).
2. Open <http://localhost:4000> in a browser. The widget shows its greeting,
   not a "not connected" notice.
3. Type *"what is the data retention policy"*. Within a few seconds, a
   grounded answer appears — the same text step 3d returned, now in the UI.
4. Type something the corpus has nothing on (*"who won the 2024 world cup"*).
   You get an honest "I don't have anything on that", **not** a made-up
   answer.
5. Open browser devtools → Network. Confirm the only cross-origin-looking
   calls are to `localhost:4000/api/eaip/*` — **nothing** goes to `:8000`
   or `:8081` from the browser. The token is never in any response body.

That fifth check is the whole point of the architecture — see it with your
own eyes once.

---

## Step 6 — (optional) against the live deployment

Same as step 5 but the `.env` points at production:

```
EAIP_MCP_TOKEN_URL=https://aiplatform.clbgroups.com/realms/eaip/protocol/openid-connect/token
EAIP_MCP_CLIENT_ID=eaip-mcp
EAIP_MCP_CLIENT_SECRET=<the PRODUCTION eaip-mcp secret — different from local>
EAIP_MCP_URL=https://aiplatform.clbgroups.com/mcp
```

The production `eaip-mcp` client already has a secret and a configured
identity (CLAUDE.md: "MCP was proven working end-to-end with a real token").
You need that secret from whoever holds the production Keycloak admin.

This is the roadmap's open Phase 12 DoD item: *"the three packages exercised
against the live production MCP endpoint with a real `eaip-mcp` credential."*

---

## What "done" looks like after this

- [ ] Step 3 (raw curl) returns a grounded answer
- [ ] Step 5 (local demo) — the widget answers in a browser, and devtools
      confirms no browser→EAIP calls and no token in any body
- [ ] Step 6 (live) — same, against `aiplatform.clbgroups.com`
- [ ] then: pick the first real integration (IoT) and follow SETUP.md for real

---

## The user journey, for reference

### The integrating developer — once, ~15 minutes

1. Asks the EAIP admin for a scoped Keycloak client → gets 4 values.
2. `npm install @eaip/widget @eaip/client @eaip/proxy-endpoint`.
3. Puts the 4 values in their backend `.env`.
4. Adds one line: `app.use("/api/eaip", eaipProxyRouter())`.
5. Drops `<EaipChat basePath="/api/eaip" />` into a React page.

### The end user — a WMS operator, every day

1. Opens their WMS. A chat panel is there, part of their app.
2. Asks a question in plain language.
3. Behind the scenes: browser → their WMS backend → (cached token) →
   EAIP `/mcp` → `search_knowledge` → grounded answer back.
4. Reads the answer, or an honest "nothing on that" — never a guess.
5. Never sees a login, a token, EAIP's URL, or Keycloak.

**What the end user cannot do in this slice:** every message from a given
integration reaches EAIP as *one* service-account identity — one tenant, one
label set, for all of that app's users. Per-end-user identity is a later
phase (`/api/eaip/session` is where it would attach).
