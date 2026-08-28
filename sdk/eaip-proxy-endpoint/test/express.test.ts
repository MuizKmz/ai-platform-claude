/**
 * The Express adapter, driven end to end: a real Express app with the router
 * mounted, real HTTP over a loopback socket, fake Keycloak and fake EAIP
 * behind it.
 *
 * Skips itself if `express` is not installed — it is an optional peer, and the
 * framework-agnostic core is covered by handler.test.ts regardless.
 */

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import type { AddressInfo } from "node:net";
import { after, before, test } from "node:test";

import { MCP_URL, TOKEN_URL, makeFakes, testConfig } from "./fakes.ts";

const require = createRequire(import.meta.url);

let express: undefined | (() => ExpressApp);
try {
  express = require("express") as () => ExpressApp;
} catch {
  express = undefined;
}

interface ExpressApp {
  use(path: string, router: unknown): void;
  listen(port: number, cb: () => void): { address(): AddressInfo | string | null; close(cb?: () => void): void };
}

const maybe = express ? test : test.skip;

let server: { close(cb?: () => void): void } | null = null;
let baseUrl = "";

before(async () => {
  if (!express) return;
  const { eaipProxyRouter } = await import("../src/express.ts");
  const fakes = makeFakes({
    tokenUrl: TOKEN_URL,
    mcpUrl: MCP_URL,
    eaipResult: { tools: [{ name: "search_knowledge" }] },
  });
  const app = express();
  app.use("/api/eaip", eaipProxyRouter({ config: testConfig(), fetch: fakes.fetch }));
  await new Promise<void>((resolve) => {
    const s = app.listen(0, () => {
      const addr = s.address() as AddressInfo;
      baseUrl = `http://127.0.0.1:${addr.port}`;
      server = s;
      resolve();
    });
  });
});

after(() => {
  server?.close();
});

maybe("POST /api/eaip/session -> 200 { ok: true }", async () => {
  const res = await fetch(`${baseUrl}/api/eaip/session`, { method: "POST" });
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { ok: true });
});

maybe("POST /api/eaip/mcp forwards tools/list and returns EAIP's result", async () => {
  const res = await fetch(`${baseUrl}/api/eaip/mcp`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 7, method: "tools/list", params: {} }),
  });
  assert.equal(res.status, 200);
  const body = (await res.json()) as { id: number; result: { tools: unknown[] } };
  assert.equal(body.id, 7);
  assert.equal(body.result.tools.length, 1);
});

maybe("POST /api/eaip/mcp with a junk body -> 400", async () => {
  const res = await fetch(`${baseUrl}/api/eaip/mcp`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nope: true }),
  });
  assert.equal(res.status, 400);
});

maybe("an unconfigured router mounts and returns 503", async () => {
  if (!express) return;
  for (const key of [
    "EAIP_MCP_TOKEN_URL",
    "EAIP_MCP_CLIENT_ID",
    "EAIP_MCP_CLIENT_SECRET",
    "EAIP_MCP_URL",
  ]) {
    delete process.env[key];
  }
  const { eaipProxyRouter } = await import("../src/express.ts");
  const app = express();
  // No config, no env — should mount and 503 rather than throw.
  app.use("/api/eaip", eaipProxyRouter({}));
  const s = await new Promise<ReturnType<ExpressApp["listen"]>>((resolve) => {
    const srv = app.listen(0, () => resolve(srv));
  });
  try {
    const addr = s.address() as AddressInfo;
    const res = await fetch(`http://127.0.0.1:${addr.port}/api/eaip/session`, { method: "POST" });
    assert.equal(res.status, 503);
  } finally {
    s.close();
  }
});
