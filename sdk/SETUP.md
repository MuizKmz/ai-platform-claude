# Adding the EAIP chat widget to your app

This is the install tutorial. Design and rationale are in
[docs/CHAT_WIDGET_SDK.md](../docs/CHAT_WIDGET_SDK.md); this is the steps.

**What you get:** a `<EaipChat />` React component that answers questions from
the knowledge and data EAIP has connected for your workspace — the same tools
the EAIP console and MCP clients use, filtered by the same tenant and label
rules.

**The shape:** your app's browser talks only to *your app's own backend*. Your
backend holds a service-account secret, exchanges it for a short-lived token,
and forwards the calls to EAIP. The browser never holds a token and never
talks to EAIP or Keycloak directly. This is why it works from any origin
without EAIP needing a CORS or routing change per integration.

```
your browser  ──►  your backend  ──►  Keycloak (token)
  <EaipChat/>       (this SDK's           │
                     proxy snippet)  ──►  EAIP /mcp
```

---

## 1. Get a Keycloak client from the EAIP admin

Ask whoever runs EAIP for a **service-account client** scoped to what this
integration should see. They follow
[infra/keycloak/MCP-SETUP.md](../infra/keycloak/MCP-SETUP.md) — either reusing
`eaip-mcp` or registering a new client (e.g. `eaip-mcp-wms`) with its own
tenant and labels.

They give you four values:

| You receive | Example |
|---|---|
| Token endpoint | `https://aiplatform.clbgroups.com/realms/eaip/protocol/openid-connect/token` |
| Client id | `eaip-mcp-wms` |
| Client secret | *(treat like a password)* |
| EAIP MCP URL | `https://aiplatform.clbgroups.com/mcp` |

> **On the MCP URL:** `/mcp` is publicly routed on the production deployment
> (verified with `curl -X POST https://aiplatform.clbgroups.com/mcp` →
> `405` for a bare GET, meaning the route reaches the backend rather than
> falling through to the frontend). Your backend calls this URL directly,
> the same as any other server-to-server HTTPS call — no private network
> path, internal proxy rule, or tunnel needed. Your *browser* never calls it
> at all; only your backend does.

---

## 2. Mount the proxy on your backend

```bash
npm install @eaip/proxy-endpoint @eaip/client @eaip/widget
```

Put the four values in your backend's `.env`:

```
EAIP_MCP_TOKEN_URL=https://aiplatform.clbgroups.com/realms/eaip/protocol/openid-connect/token
EAIP_MCP_CLIENT_ID=eaip-mcp-wms
EAIP_MCP_CLIENT_SECRET=...          # never commit this
EAIP_MCP_URL=https://aiplatform.clbgroups.com/mcp
```

### Express

```ts
import express from "express";
import { eaipProxyRouter } from "@eaip/proxy-endpoint/express";

const app = express();
app.use("/api/eaip", eaipProxyRouter());   // reads the four env vars
```

That mounts `POST /api/eaip/session` and `POST /api/eaip/mcp`.

### Any other framework

```ts
import { createEaipProxy, configFromEnv } from "@eaip/proxy-endpoint";

const result = configFromEnv();
if ("missing" in result) throw new Error(`EAIP not configured: ${result.missing}`);
const proxy = createEaipProxy(result.config);

// In your route handlers, with the JSON body already parsed:
//   POST /api/eaip/session  ->  proxy.session({ body })
//   POST /api/eaip/mcp      ->  proxy.mcp({ body })
// Each returns { status, body }; send them as-is.
```

---

## 3. Drop in the component

```tsx
import { EaipChat } from "@eaip/widget";

export function SupportPanel() {
  return (
    <div style={{ height: 480, width: 380 }}>
      <EaipChat basePath="/api/eaip" />
    </div>
  );
}
```

`basePath` must match where you mounted the proxy in step 2. It is a path on
your own origin — keep it relative.

That's the whole install.

---

## Options

```tsx
<EaipChat
  basePath="/api/eaip"
  greeting="Ask me about warehouse operations."
  placeholder="Type a question…"
  // Which EAIP tool a message calls. Default: search_knowledge / query.
  tool="query_iot_test"
  argument="question"
  // Your own analytics hook. Never receives a token.
  onExchange={({ question, ok }) => track("eaip_chat", { ok })}
  // Sizing and theming go here:
  style={{
    height: 480,
    ["--eaip-color-accent" as string]: "#0b5cff",
  }}
/>
```

### Theming

Every colour is a CSS custom property with a sensible light/dark default. Set
any of these in `style` (or on a parent):

`--eaip-color-bg`, `--eaip-color-fg`, `--eaip-color-muted`,
`--eaip-color-accent`, `--eaip-color-user-bg`, `--eaip-color-border`,
`--eaip-color-error`, `--eaip-radius-base`, `--eaip-font-family`.

The widget injects one `<style>` tag, scoped entirely to `.eaip-widget`. It
does not pull in a CSS framework and does not affect the rest of your page.

### Building your own UI

`@eaip/widget` exports `useEaipChat` — the same hook `<EaipChat />` is built
on. It gives you `messages`, `status`, `pending`, `error`, `send`, and
`reset`. Or drop to `@eaip/client` directly (`new EaipClient({ basePath })`,
then `client.ask("search_knowledge", { query })`) and render however you like.

---

## Did it work?

1. `curl -X POST http://localhost:PORT/api/eaip/session` → `{"ok":true}`.
   A `503` means the four env vars are missing or the secret is wrong.
2. Load the page with the widget. It should show its greeting, not a
   "not connected yet" notice.
3. Ask something the workspace's knowledge covers. You should get a grounded
   answer, or an honest "I don't have anything on that" — never a made-up one.

If step 1 works but the widget shows "not connected", the browser is not
reaching `basePath` on your origin — check the path matches and that your
backend is actually serving it.

---

## What this does not do yet

- **One identity per integration.** Every message from your app reaches EAIP as
  the single service-account identity your Keycloak client is bound to — one
  tenant, one label set, for all your users. Per-end-user identity is a
  planned next phase.
- **No streaming.** The answer appears when it is ready, not token by token.
- **`query_*` tools over this path answer only templated questions** — status
  lookups, reviewed metric mappings. A free-text database question that needs
  a model to draft SQL is refused with an explanation (see
  [infra/keycloak/MCP-SETUP.md](../infra/keycloak/MCP-SETUP.md)). Use
  `search_knowledge` for open questions.
