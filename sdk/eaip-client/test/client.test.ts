import assert from "node:assert/strict";
import { test } from "node:test";

import { EaipClient } from "../src/client.ts";
import {
  EaipAuthError,
  EaipNotConfiguredError,
  EaipToolError,
} from "../src/errors.ts";
import { RpcCode } from "../src/jsonrpc.ts";
import { makeFakeProxy } from "./fake-proxy.ts";

const SEARCH_TOOL = {
  name: "search_knowledge",
  description: "Search the knowledge base.",
  inputSchema: {
    type: "object" as const,
    properties: { query: { type: "string" as const, description: "the question" } },
    required: [] as string[],
  },
};

test("startSession resolves when the proxy is configured", async () => {
  const proxy = makeFakeProxy();
  const client = new EaipClient({ fetch: proxy.fetch });
  await client.startSession();
  assert.equal(proxy.calls.at(-1)?.url, "/api/eaip/session");
});

test("startSession throws EaipNotConfiguredError on a 503", async () => {
  const proxy = makeFakeProxy({ notConfigured: true });
  const client = new EaipClient({ fetch: proxy.fetch });
  await assert.rejects(() => client.startSession(), EaipNotConfiguredError);
});

test("startSession throws EaipAuthError when Keycloak refused the app's credential", async () => {
  const proxy = makeFakeProxy({ sessionUnauthorized: true });
  const client = new EaipClient({ fetch: proxy.fetch });
  await assert.rejects(() => client.startSession(), EaipAuthError);
});

test("listTools returns the tools array from tools/list", async () => {
  const proxy = makeFakeProxy({
    methods: { "tools/list": () => ({ tools: [SEARCH_TOOL] }) },
  });
  const client = new EaipClient({ fetch: proxy.fetch });
  const tools = await client.listTools();
  assert.equal(tools.length, 1);
  assert.equal(tools[0]?.name, "search_knowledge");
});

test("ask returns the tool's text on success", async () => {
  const proxy = makeFakeProxy({
    methods: {
      "tools/call": (params) => {
        assert.equal(params.name, "search_knowledge");
        assert.deepEqual(params.arguments, { query: "refund policy" });
        return {
          content: [{ type: "text", text: "Refunds are processed in 5 days." }],
          isError: false,
        };
      },
    },
  });
  const client = new EaipClient({ fetch: proxy.fetch });
  const answer = await client.ask("search_knowledge", { query: "refund policy" });
  assert.equal(answer, "Refunds are processed in 5 days.");
});

test("ask throws EaipToolError when the tool reports isError", async () => {
  const proxy = makeFakeProxy({
    methods: {
      "tools/call": () => ({
        content: [{ type: "text", text: "I could not reach the IoT database." }],
        isError: true,
      }),
    },
  });
  const client = new EaipClient({ fetch: proxy.fetch });
  await assert.rejects(
    () => client.ask("query_iot", { question: "how many devices" }),
    (err: unknown) => {
      if (!(err instanceof EaipToolError)) throw err;
      assert.equal(err.toolName, "query_iot");
      assert.match(err.message, /could not reach/);
      return true;
    },
  );
});

test("callTool returns the raw result including isError, without throwing", async () => {
  const proxy = makeFakeProxy({
    methods: {
      "tools/call": () => ({
        content: [{ type: "text", text: "nope" }],
        isError: true,
      }),
    },
  });
  const client = new EaipClient({ fetch: proxy.fetch });
  const result = await client.callTool("query_iot");
  assert.equal(result.isError, true);
});

test("a JSON-RPC UNAUTHORIZED becomes EaipAuthError with the code", async () => {
  const proxy = makeFakeProxy({
    methods: {
      "tools/list": () => {
        throw { code: RpcCode.UNAUTHORIZED, message: "Unauthorized" };
      },
    },
  });
  const client = new EaipClient({ fetch: proxy.fetch });
  await assert.rejects(
    () => client.listTools(),
    (err: unknown) => {
      if (!(err instanceof EaipAuthError)) throw err;
      assert.equal(err.code, RpcCode.UNAUTHORIZED);
      return true;
    },
  );
});

test("a JSON-RPC RATE_LIMITED becomes EaipAuthError with a retry-ish message", async () => {
  const proxy = makeFakeProxy({
    methods: {
      "tools/call": () => {
        throw { code: RpcCode.RATE_LIMITED, message: "slow down" };
      },
    },
  });
  const client = new EaipClient({ fetch: proxy.fetch });
  await assert.rejects(
    () => client.ask("search_knowledge", { query: "x" }),
    (err: unknown) => {
      if (!(err instanceof EaipAuthError)) throw err;
      assert.equal(err.code, RpcCode.RATE_LIMITED);
      assert.match(err.message, /rate limit/i);
      return true;
    },
  );
});

test("an unreachable proxy path is reported as not-configured, not a raw network error", async () => {
  const proxy = makeFakeProxy();
  // Point the client somewhere the fake does not answer.
  const client = new EaipClient({ fetch: proxy.fetch, basePath: "/nowhere" });
  await assert.rejects(() => client.listTools(), EaipNotConfiguredError);
});

test("basePath is honoured and trailing slash is tolerated", async () => {
  const proxy = makeFakeProxy({ basePath: "/custom/eaip" });
  const client = new EaipClient({ fetch: proxy.fetch, basePath: "/custom/eaip/" });
  await client.startSession();
  assert.equal(proxy.calls.at(-1)?.url, "/custom/eaip/session");
});

test("a hung call aborts at the timeout", async () => {
  const proxy = makeFakeProxy({ hang: true });
  const client = new EaipClient({ fetch: proxy.fetch, timeoutMs: 20 });
  await assert.rejects(
    () => client.listTools(),
    (err: unknown) => {
      assert.match((err as Error).message, /timed out/);
      return true;
    },
  );
});

test("request ids increment across calls", async () => {
  const seen: unknown[] = [];
  const proxy = makeFakeProxy({
    methods: {
      "tools/list": () => {
        return { tools: [] };
      },
    },
  });
  const client = new EaipClient({ fetch: proxy.fetch });
  await client.listTools();
  await client.listTools();
  for (const call of proxy.calls) {
    if (call.url.endsWith("/mcp")) seen.push((call.body as { id: number }).id);
  }
  assert.deepEqual(seen, [1, 2]);
});
