/**
 * The framework-agnostic core.
 *
 * `createEaipProxy(config)` returns two async functions — `session` and `mcp` —
 * each taking a minimal request shape and returning a minimal response shape.
 * `./express.ts` is a thin adapter; a Fastify / Koa / Next-route adapter is the
 * same handful of lines.
 *
 * What each does:
 *
 *   session(req)  -> 200 { ok: true }         once a token can be obtained
 *                    503 { error }             if config is missing / refused
 *
 *   mcp(req)      -> forwards req.body (a JSON-RPC object) to EAIP /mcp with a
 *                    cached bearer token, returns EAIP's response verbatim.
 *                    503 if this proxy is not configured.
 *                    502 if EAIP is unreachable.
 *
 * The token never appears in any response this returns. It is attached to the
 * outbound request to EAIP and nowhere else.
 */

import type { EaipProxyConfig } from "./config.ts";
import { TokenCache, TokenError } from "./token.ts";

/** The slice of an HTTP request this core needs. Adapters map their framework's
 *  request onto this. */
export interface ProxyRequest {
  /** The already-parsed JSON body. Adapters are responsible for parsing. */
  body: unknown;
}

export interface ProxyResponse {
  status: number;
  /** A JSON-serialisable body. */
  body: unknown;
}

export interface EaipProxy {
  session(req: ProxyRequest): Promise<ProxyResponse>;
  mcp(req: ProxyRequest): Promise<ProxyResponse>;
}

const JSONRPC_METHODS_ALLOWED = new Set(["initialize", "tools/list", "tools/call", "ping"]);

export function createEaipProxy(
  config: EaipProxyConfig,
  fetchImpl: typeof fetch = fetch,
): EaipProxy {
  const tokens = new TokenCache(config, fetchImpl);
  const timeoutMs = config.timeoutMs ?? 30_000;

  async function session(): Promise<ProxyResponse> {
    try {
      await tokens.get();
      return { status: 200, body: { ok: true } };
    } catch (err) {
      return notConfigured(err);
    }
  }

  async function mcp(req: ProxyRequest): Promise<ProxyResponse> {
    // Shape check. The widget's client always sends a well-formed request; a
    // malformed one is a bug or a probe, and a flat 400 is the right answer.
    if (!isJsonRpcRequest(req.body)) {
      return {
        status: 400,
        body: { error: "Body must be a JSON-RPC 2.0 request object." },
      };
    }

    // Only the four methods EAIP's MCP server implements. This proxy is not a
    // general tunnel — it exists to carry the widget's calls, and the widget
    // makes exactly these. Anything else is refused here rather than forwarded.
    if (!JSONRPC_METHODS_ALLOWED.has(req.body.method)) {
      return {
        status: 400,
        body: {
          jsonrpc: "2.0",
          id: req.body.id ?? null,
          error: { code: -32601, message: `Method not permitted through this proxy: ${req.body.method}` },
        },
      };
    }

    let token: string;
    try {
      token = await tokens.get();
    } catch (err) {
      return notConfigured(err);
    }

    const forwarded = await forwardToEaip(req.body, token);

    // One retry on Unauthorized: the cached token may have been revoked or the
    // clock may have drifted. Drop it and try once with a fresh one. A second
    // Unauthorized is genuine and passed through.
    if (forwarded.status === 401) {
      tokens.invalidate();
      try {
        const fresh = await tokens.get();
        return await forwardToEaip(req.body, fresh);
      } catch (err) {
        return notConfigured(err);
      }
    }

    return forwarded;
  }

  async function forwardToEaip(rpcBody: unknown, token: string): Promise<ProxyResponse> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    let response: Response;
    try {
      response = await fetchImpl(config.mcpUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(rpcBody),
        signal: controller.signal,
      });
    } catch (cause) {
      const reason =
        cause instanceof DOMException && cause.name === "AbortError"
          ? `timed out after ${timeoutMs}ms`
          : "network error";
      return {
        status: 502,
        body: {
          jsonrpc: "2.0",
          id: idOf(rpcBody),
          error: { code: -32603, message: `Could not reach EAIP (${reason}).` },
        },
      };
    } finally {
      clearTimeout(timer);
    }

    // Pass EAIP's response through as-is. Its status codes and JSON-RPC error
    // bodies are already what the widget's client expects to parse — this proxy
    // does not reinterpret them, except for the 401 retry above.
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      body = {
        jsonrpc: "2.0",
        id: idOf(rpcBody),
        error: { code: -32603, message: `EAIP returned a non-JSON response (HTTP ${response.status}).` },
      };
    }
    return { status: response.status, body };
  }

  return { session, mcp };
}

function notConfigured(err: unknown): ProxyResponse {
  // Every failure to obtain a token collapses to 503 "not configured" from the
  // widget's point of view — whether the secret is missing, wrong, or Keycloak
  // is down, the integrator's action is the same: check the setup. The specific
  // reason is in the response body for their logs, never anything sensitive.
  const message =
    err instanceof TokenError
      ? err.message
      : "The EAIP connection could not be established.";
  return { status: 503, body: { error: message } };
}

function isJsonRpcRequest(
  value: unknown,
): value is { jsonrpc: "2.0"; id?: unknown; method: string; params?: unknown } {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { jsonrpc?: unknown }).jsonrpc === "2.0" &&
    typeof (value as { method?: unknown }).method === "string"
  );
}

function idOf(body: unknown): string | number | null {
  const id = (body as { id?: unknown })?.id;
  return typeof id === "string" || typeof id === "number" ? id : null;
}
