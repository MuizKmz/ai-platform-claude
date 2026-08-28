/**
 * The EAIP client. It talks to ONE thing: the host app's own backend, at
 * `basePath` (default `/api/eaip`).
 *
 * It never talks to Keycloak and never talks to EAIP directly. That is not a
 * limitation to work around later — it is the design. In production EAIP's
 * `/mcp` endpoint is not publicly routed and its CORS allowlist is a single
 * origin, so a browser cannot reach it. The host backend proxies the tool
 * calls, holds the service-account secret, and is the only thing that ever
 * sees a token. See `docs/CHAT_WIDGET_SDK.md`.
 *
 * What the host backend must expose under `basePath`:
 *
 *   POST {basePath}/session   -> { ok: true } | 503 if not configured
 *   POST {basePath}/mcp       -> forwards a JSON-RPC body to EAIP /mcp,
 *                                returns EAIP's JSON-RPC response verbatim
 *
 * The `eaip-proxy-endpoint` package is a copy-paste implementation of exactly
 * that.
 */

import {
  EaipAuthError,
  EaipError,
  EaipNotConfiguredError,
  EaipToolError,
} from "./errors.ts";
import {
  RpcCode,
  textOf,
  type JsonRpcRequest,
  type JsonRpcResponse,
  type McpToolResult,
  type McpToolSpec,
  type McpToolsListResult,
} from "./jsonrpc.ts";

export interface EaipClientOptions {
  /**
   * The path on the HOST APP's own origin where the proxy endpoint is mounted.
   * Relative (`/api/eaip`) means "same origin as the page", which is the whole
   * point — no CORS, no cross-origin preflight. An absolute URL is allowed for
   * the rare case the proxy lives on a sibling API host the integrator already
   * has CORS set up for, but the default and the documented path is relative.
   */
  basePath?: string;

  /**
   * Per-request timeout in milliseconds. A tool call that runs long is usually
   * a slow connector, not a hung one; the default is generous. `0` disables it.
   */
  timeoutMs?: number;

  /**
   * Injected for testing. Defaults to the global `fetch`. Anything matching the
   * `fetch` signature works.
   */
  fetch?: typeof fetch;
}

const DEFAULT_BASE_PATH = "/api/eaip";
const DEFAULT_TIMEOUT_MS = 45_000;

export class EaipClient {
  readonly #basePath: string;
  readonly #timeoutMs: number;
  readonly #fetch: typeof fetch;
  #nextId = 1;

  constructor(options: EaipClientOptions = {}) {
    this.#basePath = (options.basePath ?? DEFAULT_BASE_PATH).replace(/\/$/, "");
    this.#timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    const f = options.fetch ?? globalThis.fetch;
    if (typeof f !== "function") {
      throw new EaipError(
        "No fetch implementation available. Pass `fetch` in options, or run somewhere fetch is global.",
      );
    }
    // Bind so a passed-in `globalThis.fetch` keeps its receiver.
    this.#fetch = f.bind(globalThis);
  }

  /**
   * Confirm the proxy is wired and a token can be obtained. Call this once when
   * the widget mounts so it can show "not configured" cleanly instead of
   * failing on the user's first message.
   *
   * Resolves on success; throws `EaipNotConfiguredError` if the proxy is
   * missing or reports no credential, `EaipAuthError` if the credential is
   * present but Keycloak refused it.
   */
  async startSession(): Promise<void> {
    const response = await this.#post("/session", {});
    if (response.status === 503) {
      throw new EaipNotConfiguredError();
    }
    if (response.status === 401 || response.status === 403) {
      throw new EaipAuthError(
        "The app's EAIP credential was refused by the identity provider.",
      );
    }
    if (!response.ok) {
      throw this.#notConfiguredFromStatus(response.status);
    }
  }

  /**
   * The tools the configured identity may use. Read-only and label-filtered by
   * EAIP — this is whatever `tools/list` returns for the service account the
   * host backend authenticates as. Never includes write tools (EAIP excludes
   * them from the MCP surface by type).
   */
  async listTools(): Promise<McpToolSpec[]> {
    const result = await this.#rpc<McpToolsListResult>("tools/list", {});
    return result.tools;
  }

  /**
   * Invoke one tool. Returns the raw MCP result (including `isError`) so a
   * caller that wants to render a failed tool as a message rather than an
   * exception can. `ask` is the throwing convenience on top.
   */
  async callTool(name: string, args: Record<string, string> = {}): Promise<McpToolResult> {
    return this.#rpc<McpToolResult>("tools/call", { name, arguments: args });
  }

  /**
   * The one-call convenience the widget uses: run a tool, and either return its
   * text or throw `EaipToolError` if it failed. Most integrations call
   * `ask("search_knowledge", { query })`.
   */
  async ask(name: string, args: Record<string, string> = {}): Promise<string> {
    const result = await this.callTool(name, args);
    const text = textOf(result);
    if (result.isError) {
      throw new EaipToolError(name, text || "The tool reported an error with no message.");
    }
    return text;
  }

  // --- internals ----------------------------------------------------------

  async #rpc<T>(method: string, params: Record<string, unknown>): Promise<T> {
    const body: JsonRpcRequest = {
      jsonrpc: "2.0",
      id: this.#nextId++,
      method,
      params,
    };

    const response = await this.#post("/mcp", body);

    // The proxy returns 503 only for its own missing configuration; a refusal
    // from EAIP comes back as HTTP 200 (or 401) with a JSON-RPC error inside.
    if (response.status === 503) {
      throw new EaipNotConfiguredError();
    }
    if (!response.ok && response.status !== 401) {
      // 502/504 from the proxy trying to reach EAIP, or anything else
      // unexpected. Treat as "the connection is not working" rather than
      // inventing a cause.
      throw this.#notConfiguredFromStatus(response.status);
    }

    let payload: JsonRpcResponse<T>;
    try {
      payload = (await response.json()) as JsonRpcResponse<T>;
    } catch {
      throw new EaipError(`EAIP returned a non-JSON response (HTTP ${response.status}).`);
    }

    if (payload.error) {
      throw this.#errorFromRpc(payload.error.code, payload.error.message);
    }
    if (payload.result === undefined) {
      throw new EaipError("EAIP returned a response with neither a result nor an error.");
    }
    return payload.result;
  }

  #errorFromRpc(code: number, message: string): EaipError {
    switch (code) {
      case RpcCode.UNAUTHORIZED:
        return new EaipAuthError("EAIP rejected the request.", code);
      case RpcCode.RATE_LIMITED:
        return new EaipAuthError(
          "This app has hit EAIP's rate limit. Try again shortly.",
          code,
        );
      case RpcCode.OVER_BUDGET:
        return new EaipAuthError(
          "This app has reached its EAIP usage budget for today.",
          code,
        );
      default:
        // METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR — a bug in the
        // caller or the server, not something an end user can act on. Surface
        // the message; it does not carry secrets (EAIP's do not).
        return new EaipError(message || `EAIP error ${code}.`);
    }
  }

  #notConfiguredFromStatus(status: number): EaipError {
    return new EaipNotConfiguredError(
      `The app's EAIP proxy responded with HTTP ${status}. Check it is mounted at "${this.#basePath}" and configured.`,
    );
  }

  async #post(path: string, body: unknown): Promise<Response> {
    const url = `${this.#basePath}${path}`;
    const controller = this.#timeoutMs > 0 ? new AbortController() : null;
    const timer =
      controller && this.#timeoutMs > 0
        ? setTimeout(() => controller.abort(), this.#timeoutMs)
        : null;

    try {
      return await this.#fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        ...(controller ? { signal: controller.signal } : {}),
      });
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") {
        throw new EaipError(`EAIP request timed out after ${this.#timeoutMs}ms.`);
      }
      // A network-level failure reaching the host's own backend. In a browser
      // this is also what a CORS rejection looks like — but the whole design is
      // that `basePath` is same-origin, so if this fires it is genuinely
      // unreachable, which is a configuration problem.
      throw new EaipNotConfiguredError(
        `Could not reach the app's EAIP proxy at "${url}".`,
      );
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
}
