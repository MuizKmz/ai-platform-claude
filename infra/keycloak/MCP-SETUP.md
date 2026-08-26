# Connecting an external AI to EAIP over MCP

This is how another AI client — Claude Desktop, Claude Code, another agent
framework, anything that speaks MCP — gets its own credential and calls EAIP's
tools directly. Every step below was run against a live Keycloak and a live
EAIP backend while writing this, not assembled from reading the code.

Background: [ADR 0007](../../docs/adr/0007-mcp-implemented-directly-rather-than-with-the-sdk.md)
(why MCP exists as a second front door) and the addendum in
[ADR 0009](../../docs/adr/0009-keycloak-for-identity.md) (why this needed its
own fix — the short version: MCP's audience mapper was accidentally shared with
the console's, and `principal_from_mcp_token` had never been taught to verify a
Keycloak token at all).

## What this gives an external client

Read access to whatever the identity you configure is allowed to see — the
same `search_knowledge` and `query_*` tools the console offers, filtered by the
same tenant and label checks. **Never write tools.** An MCP client has no
approval queue, so write-capable tools are excluded by type, not by name —
see `tool_bridge.py`.

## Step 1 — the service account needs an identity

`eaip-mcp` already has `serviceAccountsEnabled: true` in the realm file. What
the file does **not** — and, by the same reasoning that keeps users out of it,
should not — contain is *which* tenant that service account acts as. That is
an access decision, made once, by hand, the same way creating the first admin
user is.

In the Keycloak admin console (`http://localhost:8081/admin` → realm **eaip**):

1. **Clients → eaip-mcp → Service accounts roles tab** confirms the service
   account user exists: `service-account-eaip-mcp`.
2. **Users → service-account-eaip-mcp → Details tab.** Set:
   - **Email**: something identifiable, e.g. `service-account-eaip-mcp@eaip.local`,
     with **Email verified** on. Without this, every token from this client
     fails with *"token is missing required identity claims"* — a
     client-credentials token has no human behind it to already have an email,
     and `Principal.email` is not optional.
3. Still on that user, the **EAIP tenant id** and **EAIP permission labels**
   fields (declared attributes, so they show as ordinary form fields — see the
   main README) — fill in:
   - **EAIP tenant id**: which tenant's data this client may reach.
   - **EAIP permission labels**: e.g. `public`, `iot` — whatever this
     integration should see. Same meaning as a user's labels; this is not a
     separate permission model.
4. Save.

There is deliberately no default here. A service account with no `tenant_id`
is refused by the API rather than falling back to "no tenant" meaning "every
tenant" — invariant #1, unchanged for a machine credential.

## Step 2 — get the client secret

**Clients → eaip-mcp → Credentials tab → Client secret.** Copy it. Treat it
the way you would a password: it is what lets anything holding it act as this
identity, for whatever that identity has been scoped to see.

## Step 3 — get a token

```bash
curl -s -X POST http://localhost:8081/realms/eaip/protocol/openid-connect/token \
  -d "client_id=eaip-mcp" \
  -d "client_secret=<the secret from step 2>" \
  -d "grant_type=client_credentials"
```

The response's `access_token` is what an MCP client sends. It lives 5 minutes
(the realm's `accessTokenLifespan`) — long enough to connect and work, short
enough that revocation (disabling the service account, or rotating the
secret) takes effect promptly.

**Verify the token before trusting it in a client.** Decode it (e.g. at
jwt.io, or `python -c "import jwt; print(jwt.decode(t, options={'verify_signature': False}))"`)
and confirm:

- `"aud": "eaip-api-mcp"` — **not** `eaip-api`. If you see `eaip-api`, the
  client is still attached to `eaip-console-audience` or the old shared
  mapper; re-check its **Client scopes** tab.
- `"https://eaip.dev/tenant_id"` is set, matching a real tenant.
- `"https://eaip.dev/labels"` contains what you set in Step 1.

## Step 4 — point an MCP client at it

The endpoint is `POST http://<host>:8000/mcp`, speaking JSON-RPC 2.0. A
client's exact configuration syntax varies; what it needs is the URL and the
bearer token from Step 3, sent as `Authorization: Bearer <token>` on every
request.

A manual smoke test, without any MCP client, to prove the endpoint itself
before wiring up a real one:

```bash
TOKEN="<token from step 3>"

# Discover what this identity may use
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# Call one
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"search_knowledge","arguments":{"query":"refund policy"}}}'
```

`tools/list` should return only what the identity's labels authorize —
compare against what the console shows a user with the same labels. If a tool
you expect is missing, the connector behind it may be unreachable (see the
note on `query_*` tools below) rather than a permissions problem; the answer
usually says so.

## What does and does not work over MCP

**Works, and costs nothing extra:** `search_knowledge`, and any `query_*`
tool's *templated* questions — status lookups, reviewed metric mappings — the
ones answered by `tools/builtin.py`'s deterministic templates without a model
call. These run identically over MCP and the console.

**Refuses, on purpose:** a `query_*` tool's free-text path — "how many devices
do we have" phrased in a way that needs a model to draft SQL — returns
`isError: true` with an explanation, not a crash. Reasoning: the MCP *client*
is already a model. Drafting SQL server-side would mean paying for a second
model to interpret the first one's question, so the tool is offered without a
working generator (`_NullLLM` in `mcp/server.py`) and says so rather than
being silently absent or silently wrong.

**Does not exist:** write tools. Filtered out of `tools/list` and refused by
`tools/call`, by type — a write tool added in a later phase is excluded
automatically, not by someone remembering to add its name to a filter.

## Security properties, and how to check each one yourself

**A console token cannot be used here.** Audience is checked, not merely
present:

```bash
CONSOLE_TOKEN="<a token minted for eaip-console, e.g. from the browser>"
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $CONSOLE_TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
# -> {"error":{"code":-32001,"message":"Unauthorized"}}
```

**An MCP token cannot be used against the console API**, the same guarantee
in reverse:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/v1/me \
  -H "Authorization: Bearer $TOKEN"
# -> 401
```

**Labels are enforced the same as the console.** A service account with only
`public` sees only `search_knowledge`; add `iot` and `query_iot_test` appears.
Nothing about MCP widens what an identity may reach — `registry.invoke` is the
one function both front doors call, per ADR 0007.

**The token is never forwarded onward.** Whatever arrives at `/mcp` is
verified, turned into a `Principal`, and dropped. It is not attached to any
outbound connector call, another MCP server, or the model provider —
`test_mcp_no_token_passthrough` asserts this in CI.

## Rotating or revoking

- **Revoke immediately**: disable the `eaip-mcp` client, or clear the service
  account user's `tenant_id`/`labels` attributes. The next token request fails
  outright; an already-issued token stops working within its 5-minute life.
- **Rotate the secret**: Credentials tab → Regenerate. Anything using the old
  secret starts failing on its next token request; nothing already holding a
  short-lived access token needs to change until that token expires anyway.

## What this pilot does not cover yet

- **No TLS.** This endpoint is reachable over plain HTTP, same limitation as
  the rest of Phase 11 — fine on a local network or an SSH tunnel, not fine
  once this needs to be reached from somewhere else.
- **One tenant, one label set per client.** `eaip-mcp` is a single service
  account. Multiple external integrations needing different scopes today means
  either sharing this one (same access for all of them) or registering
  additional Keycloak clients with their own `eaip-mcp-audience` scope and
  their own service-account attributes — not yet automated, and not yet
  needed at the scale this platform runs at.
