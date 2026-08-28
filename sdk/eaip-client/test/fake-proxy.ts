/**
 * A fake of the host app's proxy endpoint, as a `fetch` implementation.
 *
 * It answers `POST {base}/session` and `POST {base}/mcp` the way
 * `eaip-proxy-endpoint` will, so `eaip-client`'s tests exercise the real
 * contract without a network, a Keycloak, or a running EAIP.
 */

import { RpcCode, type JsonRpcRequest } from "../src/jsonrpc.ts";

export interface FakeProxyOptions {
  basePath?: string;
  /** 503 from every route — the proxy has no EAIP_MCP_CLIENT_SECRET. */
  notConfigured?: boolean;
  /** /session returns 401 — credential present but Keycloak refused it. */
  sessionUnauthorized?: boolean;
  /** Canned handlers per JSON-RPC method. Return the `result` object, or throw
   *  a `{ code, message }` to produce a JSON-RPC error. */
  methods?: Record<
    string,
    (params: Record<string, unknown>) => unknown
  >;
  /** Make every /mcp call hang, to test the client's timeout. */
  hang?: boolean;
}

export interface FakeProxy {
  fetch: typeof fetch;
  /** Every request the client made, newest last. */
  calls: Array<{ url: string; body: unknown }>;
}

export function makeFakeProxy(options: FakeProxyOptions = {}): FakeProxy {
  const base = (options.basePath ?? "/api/eaip").replace(/\/$/, "");
  const calls: FakeProxy["calls"] = [];

  const fetchImpl = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, body });

    if (options.hang && url === `${base}/mcp`) {
      return new Promise<Response>((_, reject) => {
        // Honour abort so the client's timeout path is what fires.
        const signal = init?.signal;
        if (signal) {
          signal.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }
      });
    }

    if (options.notConfigured) {
      return json({ error: "not configured" }, 503);
    }

    if (url === `${base}/session`) {
      if (options.sessionUnauthorized) return json({ error: "unauthorized" }, 401);
      return json({ ok: true }, 200);
    }

    if (url === `${base}/mcp`) {
      const req = body as JsonRpcRequest;
      const handler = options.methods?.[req.method];
      if (!handler) {
        return json(
          {
            jsonrpc: "2.0",
            id: req.id,
            error: { code: RpcCode.METHOD_NOT_FOUND, message: `no fake for ${req.method}` },
          },
          200,
        );
      }
      try {
        const result = handler(req.params ?? {});
        return json({ jsonrpc: "2.0", id: req.id, result }, 200);
      } catch (thrown) {
        const err = thrown as { code?: number; message?: string };
        return json(
          {
            jsonrpc: "2.0",
            id: req.id,
            error: {
              code: err.code ?? RpcCode.INTERNAL_ERROR,
              message: err.message ?? "fake error",
            },
          },
          200,
        );
      }
    }

    return json({ error: "not found" }, 404);
  }) as typeof fetch;

  return { fetch: fetchImpl, calls };
}

function json(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
