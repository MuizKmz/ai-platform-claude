import assert from "node:assert/strict";
import { test } from "node:test";

import { useEaipChat } from "../src/useEaipChat.ts";
import { makeFakeProxy } from "./fake-proxy.ts";
import { mountHook, waitFor } from "./harness.ts";

const answered = (text: string, isError = false) => ({
  methods: {
    "tools/list": () => ({ tools: [] }),
    "tools/call": () => ({ content: [{ type: "text", text }], isError }),
  },
});

test("status settles on ready when the proxy is configured", async () => {
  const proxy = makeFakeProxy(answered("hi"));
  const hook = await mountHook(() => useEaipChat({ fetch: proxy.fetch }));
  // Initial value is "checking"; it resolves to "ready" once startSession
  // returns. (The fake can resolve fast enough that the first flush already
  // shows "ready", so only the settled state is asserted.)
  await waitFor(() => hook.current.status === "ready");
  hook.unmount();
});

test("status becomes not-configured on a 503 session", async () => {
  const proxy = makeFakeProxy({ notConfigured: true });
  const hook = await mountHook(() => useEaipChat({ fetch: proxy.fetch }));
  await waitFor(() => hook.current.status === "not-configured");
  hook.unmount();
});

test("status becomes unauthorized when the credential is refused", async () => {
  const proxy = makeFakeProxy({ sessionUnauthorized: true });
  const hook = await mountHook(() => useEaipChat({ fetch: proxy.fetch }));
  await waitFor(() => hook.current.status === "unauthorized");
  hook.unmount();
});

test("send appends the user message then the answer", async () => {
  const proxy = makeFakeProxy(answered("Refunds take five days."));
  const hook = await mountHook(() => useEaipChat({ fetch: proxy.fetch }));
  await waitFor(() => hook.current.status === "ready");

  await hook.act(() => hook.current.send("how long do refunds take?"));
  await waitFor(() => hook.current.messages.length === 2);

  assert.deepEqual(hook.current.messages[0], {
    role: "user",
    content: "how long do refunds take?",
  });
  assert.equal(hook.current.messages[1]?.role, "assistant");
  assert.equal(hook.current.messages[1]?.content, "Refunds take five days.");
  hook.unmount();
});

test("a failed tool becomes an assistant message flagged isError", async () => {
  const proxy = makeFakeProxy(answered("I could not reach the connector.", true));
  const hook = await mountHook(() => useEaipChat({ fetch: proxy.fetch }));
  await waitFor(() => hook.current.status === "ready");

  await hook.act(() => hook.current.send("status?"));
  await waitFor(() => hook.current.messages.length === 2);

  assert.equal(hook.current.messages[1]?.isError, true);
  assert.match(hook.current.messages[1]?.content ?? "", /could not reach the connector/);
  // Not a broken connection — still ready.
  assert.equal(hook.current.status, "ready");
  hook.unmount();
});

test("a rate-limit error sets the transient error, keeps status ready", async () => {
  const proxy = makeFakeProxy({
    methods: {
      "tools/list": () => ({ tools: [] }),
      "tools/call": () => {
        throw { code: -32002, message: "slow down" };
      },
    },
  });
  const hook = await mountHook(() => useEaipChat({ fetch: proxy.fetch }));
  await waitFor(() => hook.current.status === "ready");

  await hook.act(() => hook.current.send("x"));
  await waitFor(() => hook.current.error !== null);

  assert.match(hook.current.error ?? "", /rate limit/i);
  assert.equal(hook.current.status, "ready");
  // The user message stays; there is no assistant reply.
  assert.equal(hook.current.messages.length, 1);
  hook.unmount();
});

test("a mid-session not-configured error flips status back to not-configured", async () => {
  let configured = true;
  const base = makeFakeProxy(answered("ok"));
  const flakyFetch = (async (input: string | URL | Request, init?: RequestInit) => {
    if (!configured) return new Response(JSON.stringify({ error: "gone" }), { status: 503 });
    return base.fetch(input, init);
  }) as typeof fetch;

  const hook = await mountHook(() => useEaipChat({ fetch: flakyFetch }));
  await waitFor(() => hook.current.status === "ready");

  configured = false;
  await hook.act(() => hook.current.send("hello?"));
  await waitFor(() => hook.current.status === "not-configured");
  hook.unmount();
});

test("the configured tool and argument name are used", async () => {
  let sawName = "";
  let sawArgs: Record<string, unknown> = {};
  const proxy = makeFakeProxy({
    methods: {
      "tools/list": () => ({ tools: [] }),
      "tools/call": (params) => {
        sawName = String(params.name);
        sawArgs = params.arguments as Record<string, unknown>;
        return { content: [{ type: "text", text: "ok" }], isError: false };
      },
    },
  });
  const hook = await mountHook(() =>
    useEaipChat({ fetch: proxy.fetch, tool: "query_iot_test", argument: "question" }),
  );
  await waitFor(() => hook.current.status === "ready");
  await hook.act(() => hook.current.send("how many devices are online"));
  await waitFor(() => sawName === "query_iot_test");
  assert.deepEqual(sawArgs, { question: "how many devices are online" });
  hook.unmount();
});

test("onExchange fires with ok:true on success and ok:false on failure", async () => {
  const events: Array<{ question: string; ok: boolean }> = [];
  let failing = false;
  const base = makeFakeProxy({
    methods: {
      "tools/list": () => ({ tools: [] }),
      "tools/call": () => {
        if (failing) return { content: [{ type: "text", text: "bad" }], isError: true };
        return { content: [{ type: "text", text: "good" }], isError: false };
      },
    },
  });
  const hook = await mountHook(() =>
    useEaipChat({ fetch: base.fetch, onExchange: (i) => events.push(i) }),
  );
  await waitFor(() => hook.current.status === "ready");

  await hook.act(() => hook.current.send("one"));
  await waitFor(() => events.length === 1);
  failing = true;
  await hook.act(() => hook.current.send("two"));
  await waitFor(() => events.length === 2);

  assert.deepEqual(events, [
    { question: "one", ok: true },
    { question: "two", ok: false },
  ]);
  hook.unmount();
});

test("reset clears the transcript but not the connection status", async () => {
  const proxy = makeFakeProxy(answered("hi"));
  const hook = await mountHook(() => useEaipChat({ fetch: proxy.fetch }));
  await waitFor(() => hook.current.status === "ready");
  await hook.act(() => hook.current.send("hello"));
  await waitFor(() => hook.current.messages.length === 2);

  await hook.act(() => {
    hook.current.reset();
  });
  assert.equal(hook.current.messages.length, 0);
  assert.equal(hook.current.status, "ready");
  hook.unmount();
});

test("an empty or whitespace-only message is ignored", async () => {
  const proxy = makeFakeProxy(answered("hi"));
  const hook = await mountHook(() => useEaipChat({ fetch: proxy.fetch }));
  await waitFor(() => hook.current.status === "ready");
  await hook.act(() => hook.current.send("   "));
  assert.equal(hook.current.messages.length, 0);
  hook.unmount();
});
