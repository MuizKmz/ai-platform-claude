# The embeddable chat widget — plan

Written before any code, per the discussion that led here. This is the design
the SDK follows; it is **Phase 12** in
[IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md). The packages are built
and tested (`sdk/`); see the Status section at the end for what remains.

## The actual goal

Today, giving a new system access to EAIP over MCP means an admin manually:
creating a Keycloak service-account credential, setting its tenant/labels by
hand in the Keycloak console, and telling the integrating developer to write
their own token-fetch-and-call code from scratch (see
[infra/keycloak/MCP-SETUP.md](../infra/keycloak/MCP-SETUP.md)). That works —
it is what got the IoT platform connected — but it does not scale past a
handful of hand-held integrations, and it gives every integrator a slightly
different, hand-rolled implementation of the same HTTP calls.

The goal is a **product-shaped install**: a system's developer adds a
package, mounts one snippet on their own backend, and gets a working chat
surface talking to EAIP — the same shape as embedding Intercom or Crisp, not
the shape of integrating a raw API from scratch. This document covers the
first, concrete slice of that: the widget and its client library.
Two further pieces the same vision implies — self-serve database connection
and self-serve credential issuance — are named at the end as explicitly
**not** part of this slice.

## Why Option A (host backend holds the secret), not Option B (EAIP mints tokens)

Both were considered. The comparison that decided it:

| | Option A — host backend holds the secret | Option B — EAIP issues widget tokens |
|---|---|---|
| What's built new | The widget + client library + a copy-paste backend snippet | The same, **plus** a new authenticated endpoint on EAIP, **plus** a second secret system to secure and rotate |
| Security foundation | Reuses Keycloak — already built and proven | A bespoke system built from scratch, with less scrutiny than Keycloak has had |
| Effort per new integrating system (WMS, MES, DMS, ...) | One Keycloak credential + mount the snippet + drop in the component | The same — no scaling advantage over A |
| Risk if done wrong | Contained to the widget/host app | A new hole in EAIP's own perimeter |

Neither option lets a browser hold the secret — that was never on the table,
since anything shipped to a browser is readable by whoever is using it
(the same reasoning `eaip-console`'s PKCE flow exists for). The real choice
was where the *server-side* secret-holding logic lives, and Option A puts it
somewhere already trusted rather than building a smaller, newer version of
Keycloak ourselves for no scaling benefit.

## The browser cannot reach EAIP directly — so it doesn't

An earlier draft of this plan had the widget call `https://<eaip-host>/mcp`
directly with a short-lived token, the way `MCP-SETUP.md`'s `curl` examples
do. **That works from `curl` and from Claude Desktop. It does not work from a
browser**, for two independent reasons found by reading the live deployment
([infra/DEPLOY-aapanel.md](../infra/DEPLOY-aapanel.md),
[docker-compose.prod.yml](../docker-compose.prod.yml)):

1. **`/mcp` is not routed publicly.** The production reverse proxy forwards
   `/v1/*`, `/health`, `/realms/*`, `/admin/*`, `/resources/*` and nothing
   else. From `aiplatform.clbgroups.com` the MCP endpoint is simply
   unreachable — a request to it falls through to the frontend.
2. **CORS is a single-origin allowlist.** `CORS_ORIGINS` is exactly
   `https://aiplatform.clbgroups.com`. A widget embedded in an integrator's
   own site (`wms.example.com`) sends an `Origin` that is not on that list,
   the browser's preflight gets no `Access-Control-Allow-Origin`, and `fetch`
   throws before the request is even sent. `/mcp` has no CORS handling of its
   own; it inherits the global middleware.

`curl` sidesteps both — no `Origin` header, no preflight. The browser widget
is the one client that hits both walls.

**The fix: the browser only ever talks to its own origin.** The host app's
own backend proxies *both* the Keycloak token exchange and the MCP tool
calls. EAIP gets no new surface, no new proxy rule, no CORS change — its
`/mcp` endpoint is only ever reached server-to-server, exactly as it is
today. And the token never reaches the browser at all, which is a stronger
property than the earlier draft's (where the widget briefly held it).

## Architecture

```
┌──────────────────────┐        ┌────────────────────────────────┐        ┌───────────────────┐
│  Host app's browser   │        │      Host app's own backend      │        │  Keycloak (eaip)   │
│                       │        │      (the eaip-proxy-endpoint     │        │                    │
│  <EaipChat            │  (1)   │       snippet, mounted on theirs) │  (2)   │  client_credentials│
│    basePath=          ├───────►│                                  ├───────►│  grant, returns a  │
│    "/api/eaip" />      │        │  POST /api/eaip/session           │◄───────┤  5-min token       │
│                       │◄───────┤    -> starts a server-side session │        │                    │
│                       │  (4)   │  POST /api/eaip/mcp                │        └───────────────────┘
│                       ├───────►│    -> forwards one JSON-RPC call   │  (3)   ┌───────────────────┐
│                       │◄───────┤       with the cached token        ├───────►│  EAIP /mcp          │
└──────────────────────┘        └────────────────────────────────┘        │  (UNCHANGED — not  │
     every call is same-origin.                                            │   public, not CORS-│
     no token in the browser.                                              │   widened. only    │
                                                                          │   ever reached     │
                                                                          │   from here.)      │
                                                                          └───────────────────┘
```

1. The widget, in the host app's frontend, calls **the host app's own
   backend** at a path the host app controls (`basePath`, default
   `/api/eaip`). It never calls Keycloak, never calls EAIP, never holds a
   secret or a token.
2. The host backend (the `eaip-proxy-endpoint` snippet the SDK hands the
   integrator) exchanges its `EAIP_MCP_CLIENT_SECRET` for a short-lived
   token via the `client_credentials` grant, and caches it in memory until
   ~30s before expiry.
3. On each tool call, the host backend forwards the JSON-RPC body to EAIP's
   `/mcp` with `Authorization: Bearer <cached token>`. This is a
   server-to-server call — no browser, no `Origin`, no preflight, no CORS.
4. The result comes back to the widget through the host backend. The token
   is never in a response body, never in the browser, never on disk.

**`/api/eaip/session`** does no real work today beyond confirming the proxy
is wired and a token can be obtained — it exists so the widget can show a
clear "not configured" state instead of failing on first message. It is the
natural place to later attach a per-end-user identity (see out-of-scope).

## What ships

```
sdk/
  eaip-client/            Framework-agnostic. Talks ONLY to the host app's
                          own backend (basePath). tools/list + tools/call
                          over JSON-RPC, thin result typing, an error type
                          that distinguishes "not configured" / "unauthorized"
                          / "tool failed". No token logic — the token lives
                          on the host backend, not here.
  eaip-widget/            React component (<EaipChat />) built on eaip-client:
                          input box, message list, loading state, error
                          display. Also exports the useEaipChat hook for a
                          custom UI. A failed tool is shown as a message, not
                          hidden — same as the console treats a refusal.
  eaip-proxy-endpoint/    A tiny, copy-pasteable handler for the INTEGRATOR's
                          own backend — Express reference implementation plus
                          a framework-agnostic core (a function that takes a
                          parsed request and returns a response) so it ports
                          to Fastify/Koa/Next route handlers/etc. Holds the
                          secret, does the client_credentials grant, caches
                          the token, forwards to EAIP /mcp. Not a service
                          EAIP runs; code the integrator mounts on theirs.
  SETUP.md                The install tutorial: register a Keycloak client,
                          npm install, mount the proxy handler with four env
                          vars, drop in the component, done.
```

`eaip-client` has no React dependency — a non-React consumer (a plain script,
a different framework) can use it directly and build their own UI on top,
same as the widget does.

### Why a proxy snippet and not an EAIP-hosted endpoint

Same reasoning as Option A over Option B, one level down. An EAIP-hosted
"widget backend" would be a new public, CORS-widened, per-integrator-origin
surface on EAIP — the exact thing the two blockers above are telling us not
to build. The proxy snippet keeps every new moving part on the integrator's
side, where a mistake is contained to their app.

## What a new integration (WMS, MES, DMS, ...) actually requires

Unchanged by which system it is — this is the point of building it once:

1. **A Keycloak client**, admin-created today the same way `eaip-mcp` was
   created for IoT: either reuse `eaip-mcp` if the same tenant/labels apply,
   or register a new client (`eaip-mcp-wms`, say) with its own
   `eaip-mcp-audience` scope and its own service-account `tenant_id`/`labels`
   if this system needs different scope. A few minutes in the Keycloak
   console, per system. (See `MCP-SETUP.md` — this SDK does not change that
   procedure, it just consumes its output.)
2. **Mount `eaip-proxy-endpoint`** on their own backend, with
   `EAIP_MCP_CLIENT_ID`, `EAIP_MCP_CLIENT_SECRET`, `EAIP_MCP_TOKEN_URL`, and
   `EAIP_MCP_URL` in their own `.env`.
3. **`npm install` the widget, drop in `<EaipChat basePath="/api/eaip" />`.**

No EAIP-side code changes per integration. The package is generic; only the
Keycloak-side scope differs.

## Explicitly out of scope for this slice

Named here so nobody mistakes "the widget exists" for "the whole vision is
done":

- **Per-end-user identity.** Every message from a given integration reaches
  EAIP as the *one* service-account principal that integration's Keycloak
  client is bound to — one tenant, one label set, for all of that host app's
  users. Distinguishing "Alice in the WMS" from "Bob in the WMS" would mean
  the host backend asserting an end-user identity EAIP then trusts, which is
  a real cross-system trust decision (token exchange, or EAIP issuing scoped
  tokens after all) deserving its own ADR. `/api/eaip/session` is where that
  would attach.
- **Self-serve database connection.** A connector row (like `iot_curated`
  today) is still created by an EAIP admin through the console, by hand.
  Letting an integrating developer configure their own data source as part
  of installing the widget is a real, separate feature — it means deciding
  what a non-admin is allowed to point EAIP at, which is an access-control
  question, not a widget question.
- **Self-serve credential issuance.** Getting a scoped Keycloak client today
  is a manual admin action in Keycloak's own console. A page in EAIP's own
  admin UI that creates one on request — the "MCP Connections" idea raised
  earlier in Phase 11's work — would remove that manual step, but it means
  EAIP's backend calling Keycloak's admin API on someone's behalf, which is
  new cross-system trust deserving its own ADR, not something to fold into a
  widget's first version.
- **Streaming responses.** MCP `tools/call` is request/response, and the
  widget renders a full answer when it arrives. Token-by-token streaming
  would need a different transport than the one front door this reuses.

All are real next phases. None blocks the widget working today against the
one real integration (IoT) that already has its Keycloak credential set up
by hand.

## Testing

44 tests, Node's built-in runner, no test framework. All against fakes of
Keycloak and `/mcp` — the contract, not the live stack.

- `eaip-client` (13): against a fake of the host backend's proxy — `tools/list`
  / `tools/call`, the three error classes, timeout, id increment, `basePath`
  handling.
- `eaip-proxy-endpoint` (20): against a fake Keycloak token endpoint and a
  fake `/mcp` — token caching, single-flight refresh, expiry, the one 401
  retry, method allowlist, and a check that the service-account token never
  appears in a response body. Plus a real Express app over a loopback socket
  driving the router end to end.
- `eaip-widget` (11): the `useEaipChat` hook, mounted with `react-dom` +
  jsdom — status transitions, send/answer, a failed tool shown (not hidden),
  a rate-limit error kept transient, `onExchange`, `reset`. The `EaipChat.tsx`
  presentational shell is **not** unit-tested — Node's native runner has no
  `.tsx` support, and the shell carries no logic the hook doesn't; it is
  covered by the SETUP.md smoke and a later Playwright E2E (ADR 0006).
- Not E2E against the real stack in this slice — that needs a Keycloak client
  and a running EAIP. The SETUP.md "did it work" step is the manual smoke
  against the live deployment.

## Status

**Built and tested (branch `sdk/chat-widget`).** The plan was revised once,
on 2026-08-28, before any code: the first draft had the browser call `/mcp`
directly with a short-lived token, which cannot work (that path is not
publicly routed and `CORS_ORIGINS` is a single origin), so the host backend
proxies the tool calls too.

**Not yet done:** the SETUP.md smoke run against the live endpoint with a real
`eaip-mcp` credential, and migrating the IoT integration onto the widget as
the first real consumer. Tracked as the open Phase 12 items in
[IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md).
