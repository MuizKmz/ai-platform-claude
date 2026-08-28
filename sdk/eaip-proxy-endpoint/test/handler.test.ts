import assert from "node:assert/strict";
import { test } from "node:test";

import { configFromEnv } from "../src/config.ts";
import { createEaipProxy } from "../src/handler.ts";
import { TokenCache } from "../src/token.ts";
import { MCP_URL, TOKEN_URL, makeFakes, testConfig } from "./fakes.ts";

const rpc = (method: string, params: Record<string, unknown> = {}) => ({
  jsonrpc: "2.0" as const,
  id: 1,
  method,
  params,
});

// --- config -----------------------------------------------------------------

test("configFromEnv reports every missing variable by name", () => {
  const result = configFromEnv({});
  assert.ok("missing" in result);
  assert.deepEqual(result.missing.sort(), [
    "EAIP_MCP_CLIENT_ID",
    "EAIP_MCP_CLIENT_SECRET",
    "EAIP_MCP_TOKEN_URL",
    "EAIP_MCP_URL",
  ]);
});

test("configFromEnv builds a config when all four are present", () => {
  const result = configFromEnv({
    EAIP_MCP_TOKEN_URL: TOKEN_URL,
    EAIP_MCP_CLIENT_ID: "eaip-mcp",
    EAIP_MCP_CLIENT_SECRET: "s",
    EAIP_MCP_URL: MCP_URL,
  });
  assert.ok("config" in result);
  assert.equal(result.config.clientId, "eaip-mcp");
});

// --- session --------------------------------------------------------------

test("session returns 200 { ok: true } once a token can be obtained", async () => {
  const fakes = makeFakes({ tokenUrl: TOKEN_URL, mcpUrl: MCP_URL });
  const proxy = createEaipProxy(testConfig(), fakes.fetch);
  const out = await proxy.session({ body: {} });
  assert.equal(out.status, 200);
  assert.deepEqual(out.body, { ok: true });
});

test("session returns 503 when Keycloak refuses the credential", async () => {
  const fakes = makeFakes({ tokenUrl: TOKEN_URL, mcpUrl: MCP_URL, tokenRefused: true });
  const proxy = createEaipProxy(testConfig(), fakes.fetch);
  const out = await proxy.session({ body: {} });
  assert.equal(out.status, 503);
});

test("session returns 503 when Keycloak is unreachable", async () => {
  const fakes = makeFakes({ tokenUrl: TOKEN_URL, mcpUrl: MCP_URL, tokenNetworkError: true });
  const proxy = createEaipProxy(testConfig(), fakes.fetch);
  const out = await proxy.session({ body: {} });
  assert.equal(out.status, 503);
});

// --- mcp forwarding -------------------------------------------------------

test("mcp forwards a tools/list call to EAIP with a bearer token and returns its result", async () => {
  const fakes = makeFakes({
    tokenUrl: TOKEN_URL,
    mcpUrl: MCP_URL,
    eaipResult: { tools: [{ name: "search_knowledge" }] },
  });
  const proxy = createEaipProxy(testConfig(), fakes.fetch);
  const out = await proxy.mcp({ body: rpc("tools/list") });
  assert.equal(out.status, 200);
  assert.deepEqual((out.body as { result: unknown }).result, {
    tools: [{ name: "search_knowledge" }],
  });
  assert.equal(fakes.eaipTokensSeen[0], "token-1");
});

test("the token is cached across calls — one token request for several tool calls", async () => {
  const fakes = makeFakes({ tokenUrl: TOKEN_URL, mcpUrl: MCP_URL });
  const proxy = createEaipProxy(testConfig(), fakes.fetch);
  await proxy.mcp({ body: rpc("tools/list") });
  await proxy.mcp({ body: rpc("tools/call", { name: "search_knowledge" }) });
  await proxy.mcp({ body: rpc("ping") });
  assert.equal(fakes.tokenRequests, 1);
});

test("concurrent first calls share one token request", async () => {
  const fakes = makeFakes({ tokenUrl: TOKEN_URL, mcpUrl: MCP_URL });
  const proxy = createEaipProxy(testConfig(), fakes.fetch);
  await Promise.all([
    proxy.mcp({ body: rpc("tools/list") }),
    proxy.mcp({ body: rpc("tools/list") }),
    proxy.mcp({ body: rpc("ping") }),
  ]);
  assert.equal(fakes.tokenRequests, 1);
});

test("a stale token is refreshed", async () => {
  // A 10s token with a 60s refresh skew => stale the instant it is issued.
  const fakes = makeFakes({ tokenUrl: TOKEN_URL, mcpUrl: MCP_URL, tokenTtlSeconds: 10 });
  const proxy = createEaipProxy(
    { ...testConfig(), refreshSkewSeconds: 60 },
    fakes.fetch,
  );
  await proxy.mcp({ body: rpc("tools/list") });
  await proxy.mcp({ body: rpc("ping") });
  assert.equal(fakes.tokenRequests, 2);
});

test("a 401 from EAIP triggers exactly one retry with a fresh token", async () => {
  const fakes = makeFakes({
    tokenUrl: TOKEN_URL,
    mcpUrl: MCP_URL,
    eaipUnauthorizedFirst: 1,
  });
  const proxy = createEaipProxy(testConfig(), fakes.fetch);
  const out = await proxy.mcp({ body: rpc("tools/list") });
  assert.equal(out.status, 200);
  // First token, rejected; cache invalidated; second token, accepted.
  assert.deepEqual(fakes.eaipTokensSeen, ["token-1", "token-2"]);
  assert.equal(fakes.tokenRequests, 2);
});

test("a persistent 401 from EAIP is passed through after one retry", async () => {
  const fakes = makeFakes({
    tokenUrl: TOKEN_URL,
    mcpUrl: MCP_URL,
    eaipUnauthorizedFirst: 5,
  });
  const proxy = createEaipProxy(testConfig(), fakes.fetch);
  const out = await proxy.mcp({ body: rpc("tools/list") });
  assert.equal(out.status, 401);
  assert.equal(fakes.eaipTokensSeen.length, 2); // original + one retry, no more
});

test("EAIP unreachable becomes a 502 with a JSON-RPC error, not a throw", async () => {
  const fakes = makeFakes({ tokenUrl: TOKEN_URL, mcpUrl: MCP_URL, eaipNetworkError: true });
  const proxy = createEaipProxy(testConfig(), fakes.fetch);
  const out = await proxy.mcp({ body: rpc("tools/list") });
  assert.equal(out.status, 502);
  assert.match((out.body as { error: { message: string } }).error.message, /Could not reach EAIP/);
});

test("a non-JSON-RPC body is rejected with 400 before any token is fetched", async () => {
  const fakes = makeFakes({ tokenUrl: TOKEN_URL, mcpUrl: MCP_URL });
  const proxy = createEaipProxy(testConfig(), fakes.fetch);
  const out = await proxy.mcp({ body: { not: "jsonrpc" } });
  assert.equal(out.status, 400);
  assert.equal(fakes.tokenRequests, 0);
});

test("a method outside the four EAIP implements is refused, not forwarded", async () => {
  const fakes = makeFakes({ tokenUrl: TOKEN_URL, mcpUrl: MCP_URL });
  const proxy = createEaipProxy(testConfig(), fakes.fetch);
  const out = await proxy.mcp({ body: rpc("resources/read") });
  assert.equal(out.status, 400);
  assert.equal(fakes.eaipBodies.length, 0);
});

test("the service-account token never appears in a response body", async () => {
  const fakes = makeFakes({ tokenUrl: TOKEN_URL, mcpUrl: MCP_URL });
  const proxy = createEaipProxy(testConfig(), fakes.fetch);
  const session = await proxy.session({ body: {} });
  const call = await proxy.mcp({ body: rpc("tools/list") });
  const err502 = await createEaipProxy(
    testConfig(),
    makeFakes({ tokenUrl: TOKEN_URL, mcpUrl: MCP_URL, eaipNetworkError: true }).fetch,
  ).mcp({ body: rpc("tools/list") });

  for (const out of [session, call, err502]) {
    assert.doesNotMatch(JSON.stringify(out.body), /token-\d/);
    assert.doesNotMatch(JSON.stringify(out.body), /shhh/);
  }
});

// --- token cache unit ---------------------------------------------------------

test("TokenCache.invalidate forces a refetch", async () => {
  const fakes = makeFakes({ tokenUrl: TOKEN_URL, mcpUrl: MCP_URL });
  const cache = new TokenCache(testConfig(), fakes.fetch);
  assert.equal(await cache.get(), "token-1");
  assert.equal(await cache.get(), "token-1"); // cached
  cache.invalidate();
  assert.equal(await cache.get(), "token-2");
  assert.equal(fakes.tokenRequests, 2);
});
