# @eaip/proxy-endpoint

The host-backend half of the EAIP chat widget. You mount this on **your own**
backend. It:

1. holds your EAIP service-account secret (`EAIP_MCP_CLIENT_SECRET`),
2. exchanges it for a short-lived token via the `client_credentials` grant,
   and caches that token in memory until just before it expires,
3. forwards the widget's JSON-RPC calls to EAIP's `/mcp` with that token —
   server-to-server, so no CORS and no browser ever holds the token.

Design: [../../docs/CHAT_WIDGET_SDK.md](../../docs/CHAT_WIDGET_SDK.md).
Install steps: [../SETUP.md](../SETUP.md).

## Express

```ts
import express from "express";
import { eaipProxyRouter } from "@eaip/proxy-endpoint/express";

const app = express();
app.use("/api/eaip", eaipProxyRouter());   // reads config from process.env
```

Mounts `POST /api/eaip/session` and `POST /api/eaip/mcp`.

## Any framework

```ts
import { createEaipProxy, configFromEnv } from "@eaip/proxy-endpoint";

const result = configFromEnv();
if ("missing" in result) throw new Error(`missing: ${result.missing.join(", ")}`);
const proxy = createEaipProxy(result.config);

// with the JSON body already parsed:
app.post("/api/eaip/session", async (req, res) => {
  const out = await proxy.session({ body: req.body });
  res.status(out.status).json(out.body);
});
app.post("/api/eaip/mcp", async (req, res) => {
  const out = await proxy.mcp({ body: req.body });
  res.status(out.status).json(out.body);
});
```

## Configuration

Four required environment variables — all values your EAIP admin gives you
(see [../SETUP.md](../SETUP.md) step 1):

| Variable | |
|---|---|
| `EAIP_MCP_TOKEN_URL` | Keycloak's token endpoint for the realm |
| `EAIP_MCP_CLIENT_ID` | your Keycloak client id |
| `EAIP_MCP_CLIENT_SECRET` | the client secret — **never commit** |
| `EAIP_MCP_URL` | the address your backend reaches EAIP's `/mcp` at |

Optional: `EAIP_MCP_REFRESH_SKEW_SECONDS` (default 30),
`EAIP_MCP_TIMEOUT_MS` (default 30000).

## Behaviour worth knowing

- **Token in memory only.** A restart fetches a fresh one. It is never written
  to disk, logged, or returned in any response body.
- **One refresh at a time.** Concurrent first calls share a single token
  request.
- **One retry on `401` from EAIP** with a fresh token, then the `401` is
  passed through.
- **Only the four MCP methods** EAIP implements (`initialize`, `tools/list`,
  `tools/call`, `ping`) are forwarded. Anything else is refused here — this is
  not a general tunnel.
- **EAIP's responses pass through unchanged.** Status codes and JSON-RPC error
  bodies are what `@eaip/client` expects to parse; this proxy does not
  reinterpret them.

`express` is an **optional** peer dependency — the package root works without
it.
