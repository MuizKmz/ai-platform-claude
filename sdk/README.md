# EAIP chat widget SDK

Three packages that let another system embed a chat surface talking to EAIP,
without an EAIP engineer hand-building the integration.

| Package | What it is |
|---|---|
| [`eaip-client`](./eaip-client) | Framework-agnostic. Talks to the host app's own backend proxy — `tools/list`, `tools/call`, error typing. No token logic, no direct EAIP calls. |
| [`eaip-widget`](./eaip-widget) | React `<EaipChat />` built on `eaip-client`. Self-contained styling, no design-system dependency. Also exports the `useEaipChat` hook. |
| [`eaip-proxy-endpoint`](./eaip-proxy-endpoint) | The copy-paste backend snippet for the integrator. Holds the service-account secret, mints and caches a token, forwards MCP calls to EAIP. Express adapter + framework-agnostic core. |

**Install tutorial:** [SETUP.md](./SETUP.md).
**Design and rationale:** [../docs/CHAT_WIDGET_SDK.md](../docs/CHAT_WIDGET_SDK.md).

## The one important idea

The browser only ever talks to the host app's **own** origin. The host
backend proxies both the Keycloak token exchange and the EAIP `/mcp` calls.
EAIP gets no new public surface and no per-integration CORS change — its
`/mcp` endpoint is only ever reached server-to-server, exactly as it is today.
The service-account token never reaches the browser.

This is why the widget works from any integrator's domain: the cross-origin
problem is solved by not being cross-origin.

## Working on these

Each package is standalone (its own `package.json`, `tsconfig`, tests). There
is no workspace root — `eaip-widget` depends on `eaip-client` via a
`file:../eaip-client` path, so build `eaip-client` first.

```bash
cd eaip-client       && npm install && npm run build && npm test
cd ../eaip-proxy-endpoint && npm install && npm test && npm run build
cd ../eaip-widget    && npm install && npm test && npm run build
```

Every package: `npm test` (Node's built-in runner, no framework), `npm run
typecheck`, `npm run build` (`tsc` to `dist/`).

### Why `.ts` import specifiers in source

Node runs the `.ts` files directly for tests (its type-stripping does not
rewrite `.js`→`.ts`), and `tsc` rewrites them to `.js` on build via
`rewriteRelativeImportExtensions`. `eaip-widget`'s `EaipChat.tsx` is not
covered by Node's native runner (no `.tsx` support) — the logic lives in the
`useEaipChat` hook, which is `.ts` and fully tested; the presentational shell
is for the SETUP.md smoke and a later Playwright E2E, per
`docs/CHAT_WIDGET_SDK.md`.

## Status

**Built and tested against fakes; not yet run against the live endpoint or
wired into a real integration.** MCP itself is already proven end-to-end in
production with a real `eaip-mcp` token (`infra/keycloak/MCP-SETUP.md`) — what
remains here is the SETUP.md smoke through the proxy, then migrating the IoT
platform onto the widget as the first real consumer.
