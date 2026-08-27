# The embeddable chat widget — plan

Written before any code, per the discussion that led here. This is the design
being built; [IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md) is where
it gets slotted in once it exists.

## The actual goal

Today, giving a new system access to EAIP over MCP means an admin manually:
creating a Keycloak service-account credential, setting its tenant/labels by
hand in the Keycloak console, and telling the integrating developer to write
their own token-fetch-and-call code from scratch (see
[infra/keycloak/MCP-SETUP.md](../infra/keycloak/MCP-SETUP.md)). That works —
it is what got the IoT platform connected — but it does not scale past a
handful of hand-held integrations, and it gives every integrator a slightly
different, hand-rolled implementation of the same three HTTP calls.

The goal is a **product-shaped install**: a system's developer adds a
package, points it at a token endpoint on their own backend, and gets a
working chat surface talking to EAIP — the same shape as embedding Intercom
or Crisp, not the shape of integrating a raw API from scratch. This document
covers the first, concrete slice of that: the widget and its client library.
Two further pieces the same vision implies — self-serve database connection
and self-serve credential issuance — are named at the end as explicitly
**not** part of this slice.

## Why Option A (host backend holds the secret), not Option B (EAIP mints tokens)

Both were considered. The comparison that decided it:

| | Option A — host backend holds the secret | Option B — EAIP issues widget tokens |
|---|---|---|
| What's built new | The widget + client library only | The same, **plus** a new authenticated endpoint on EAIP, **plus** a second secret system to secure and rotate |
| Security foundation | Reuses Keycloak — already built and proven | A bespoke system built from scratch, with less scrutiny than Keycloak has had |
| Effort per new integrating system (WMS, MES, DMS, ...) | One Keycloak credential + three copy-paste steps | The same — no scaling advantage over A |
| Risk if done wrong | Contained to the widget/host app | A new hole in EAIP's own perimeter |

Neither option lets a browser hold the secret — that was never on the table,
since anything shipped to a browser is readable by whoever is using it
(the same reasoning `eaip-console`'s PKCE flow exists for). The real choice
was where the *server-side* secret-holding logic lives, and Option A puts it
somewhere already trusted rather than building a smaller, newer version of
Keycloak ourselves for no scaling benefit.

## Architecture

```
┌─────────────────────┐        ┌──────────────────────────┐        ┌─────────────────┐
│  Host app's browser  │        │   Host app's own backend  │        │  Keycloak (eaip)  │
│                      │  (1)   │                            │  (2)   │                  │
│  <EaipChat           ├───────►│  POST /api/eaip-token       ├───────►│  client_credentials│
│    tokenEndpoint=    │        │  (holds EAIP_MCP_SECRET,   │        │  grant, returns    │
│    "/api/eaip-token" │◄───────┤   calls Keycloak, returns  │◄───────┤  a 5-min token     │
│  />                  │  (3)   │   just the token)          │        │                  │
└──────────┬───────────┘        └──────────────────────────┘        └─────────────────┘
           │ (4)
           ▼
┌──────────────────────┐
│  EAIP /mcp             │
│  (the same endpoint    │
│   MCP-SETUP.md          │
│   documents)            │
└──────────────────────┘
```

1. The widget, running in the host app's own frontend, asks the host app's
   *own backend* for a token — never Keycloak directly, never holding a
   secret.
2. The host backend (code the SDK hands the integrator, not something they
   write blind) exchanges its `EAIP_MCP_SECRET` for a short-lived token.
3. The token comes back to the widget. It never touches disk, logs, or
   anything longer-lived than the request.
4. The widget calls `https://<eaip-host>/mcp` directly with that token,
   exactly as `infra/keycloak/MCP-SETUP.md`'s manual `curl` examples do
   today — the widget is a client of that same, unmodified endpoint. No new
   surface is added to EAIP itself for this slice.

## What ships

```
sdk/
  eaip-client/          Framework-agnostic: token caching/refresh,
                        tools/list, tools/call over JSON-RPC.
  eaip-widget/           React component (<EaipChat />) built on
                        eaip-client: input box, message list, loading
                        state, error display.
  eaip-token-endpoint/   A tiny, copy-pasteable Express (or equivalent)
                        handler for the INTEGRATOR's own backend — the
                        piece that holds their secret and talks to
                        Keycloak. Not a service EAIP runs; a snippet the
                        integrator mounts on theirs.
  SETUP.md               The install tutorial: npm install, mount the
                        token handler, drop in the component, done.
```

`eaip-client` has no React dependency — a non-React consumer (a plain script,
a different framework) can use it directly and build their own UI on top,
same as the widget does.

## What a new integration (WMS, MES, DMS, ...) actually requires

Unchanged by which system it is — this is the point of building it once:

1. **A Keycloak credential**, admin-created today the same way `eaip-mcp`
   was created for IoT: either reuse `eaip-mcp` if the same tenant/labels
   apply, or register a new client (`eaip-mcp-wms`, say) if this system
   needs different scope. A few minutes in the Keycloak console, per system.
2. **Mount `eaip-token-endpoint`** on their own backend, with their secret
   in their own `.env`.
3. **`npm install` the widget, drop in `<EaipChat tokenEndpoint="..." />`.**

No EAIP-side code changes per integration. The package is generic; only the
Keycloak-side scope differs.

## Explicitly out of scope for this slice

Named here so nobody mistakes "the widget exists" for "the whole vision is
done":

- **Self-serve database connection.** A connector row (like `iot_curated`
  today) is still created by an EAIP admin through the console, by hand.
  Letting an integrating developer configure their own data source as part
  of installing the widget is a real, separate feature — it means deciding
  what a non-admin is allowed to point EAIP at, which is an access-control
  question, not a widget question.
- **Self-serve credential issuance.** Getting a scoped Keycloak client
  today is a manual admin action in Keycloak's own console. A page in
  EAIP's own admin UI that creates one on request — the "MCP Connections"
  idea raised earlier in Phase 11's work — would remove that manual step,
  but it means EAIP's backend calling Keycloak's admin API on someone's
  behalf, which is new cross-system trust deserving its own ADR, not
  something to fold into a widget's first version.

Both are real next phases. Neither blocks the widget working today against
the one real integration (IoT) that already has its Keycloak credential set
up by hand.

## Status

**Not yet built.** This is the plan the implementation follows next.
