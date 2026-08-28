# @eaip/client

Framework-agnostic access to EAIP's chat tools. Talks to **one** thing: the
host app's own backend proxy (see [`@eaip/proxy-endpoint`](../eaip-proxy-endpoint)).

It never talks to Keycloak or EAIP directly — that is the design, not a
limitation. See [../../docs/CHAT_WIDGET_SDK.md](../../docs/CHAT_WIDGET_SDK.md).

```ts
import { EaipClient } from "@eaip/client";

const client = new EaipClient({ basePath: "/api/eaip" });

await client.startSession();                     // throws if the proxy is not set up
const tools = await client.listTools();          // what the configured identity may use
const answer = await client.ask("search_knowledge", { query: "refund policy" });
```

## API

- `new EaipClient({ basePath?, timeoutMs?, fetch? })` — `basePath` defaults to
  `/api/eaip` and should be a path on your own origin.
- `startSession()` — confirm the proxy is wired. Resolves, or throws
  `EaipNotConfiguredError` / `EaipAuthError`.
- `listTools()` — `McpToolSpec[]`.
- `callTool(name, args?)` — the raw `McpToolResult`, including `isError`,
  without throwing on a tool failure.
- `ask(name, args?)` — the tool's text, or throws `EaipToolError` if it failed.

## Errors

| Class | Meaning | What a caller does |
|---|---|---|
| `EaipNotConfiguredError` | the host app's proxy is missing or has no credential | "not connected yet" — an admin action |
| `EaipAuthError` | EAIP refused the call (bad token, wrong labels, rate limit, budget). `.code` carries the JSON-RPC code when there was one. | retry later, or an admin action |
| `EaipToolError` | a tool ran and reported failure — `.toolName`, `.message` (safe to show) | render as a message |
| `EaipError` | base class / anything else | generic failure |

No token handling lives here — the token is the host backend's, and never
reaches this code.
