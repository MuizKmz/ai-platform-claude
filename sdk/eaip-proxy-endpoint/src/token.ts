/**
 * The token cache.
 *
 * EAIP's access tokens live 300 seconds (`infra/keycloak/README.md`). Fetching
 * one per tool call would triple the latency of every message and hammer
 * Keycloak. So this holds one in memory and refreshes it a little before it
 * expires.
 *
 * In memory, not on disk: the token is a bearer credential and the whole point
 * of this design is that it never lands anywhere persistent. A process restart
 * just fetches a fresh one.
 *
 * One refresh at a time: several concurrent tool calls arriving with a stale
 * token would otherwise start several token requests. The in-flight promise is
 * shared.
 */

import type { EaipProxyConfig } from "./config.ts";

export class TokenError extends Error {
  /** True when Keycloak refused the credential (4xx) rather than being
   *  unreachable (network / 5xx). A refused credential is a config problem the
   *  integrator must fix; an unreachable Keycloak might just be transient. */
  readonly refused: boolean;

  constructor(message: string, refused: boolean) {
    super(message);
    this.name = "TokenError";
    this.refused = refused;
  }
}

interface CachedToken {
  value: string;
  /** Epoch ms at which we consider it stale (real expiry minus the skew). */
  staleAt: number;
}

const DEFAULT_SKEW_SECONDS = 30;
const DEFAULT_TIMEOUT_MS = 30_000;

export class TokenCache {
  readonly #config: EaipProxyConfig;
  readonly #fetch: typeof fetch;
  readonly #skewMs: number;
  readonly #timeoutMs: number;
  #cached: CachedToken | null = null;
  #inFlight: Promise<string> | null = null;

  constructor(config: EaipProxyConfig, fetchImpl: typeof fetch = fetch) {
    this.#config = config;
    this.#fetch = fetchImpl;
    this.#skewMs = (config.refreshSkewSeconds ?? DEFAULT_SKEW_SECONDS) * 1000;
    this.#timeoutMs = config.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  }

  /** A valid token, from cache or freshly fetched. Throws `TokenError`. */
  async get(): Promise<string> {
    if (this.#cached && Date.now() < this.#cached.staleAt) {
      return this.#cached.value;
    }
    this.#inFlight ??= this.#fetchToken().finally(() => {
      this.#inFlight = null;
    });
    return this.#inFlight;
  }

  /** Drop the cached token — call this after EAIP returns Unauthorized, so the
   *  next attempt fetches a fresh one rather than replaying the rejected one. */
  invalidate(): void {
    this.#cached = null;
  }

  async #fetchToken(): Promise<string> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.#timeoutMs);

    let response: Response;
    try {
      response = await this.#fetch(this.#config.tokenUrl, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          grant_type: "client_credentials",
          client_id: this.#config.clientId,
          client_secret: this.#config.clientSecret,
        }),
        signal: controller.signal,
      });
    } catch (cause) {
      const reason =
        cause instanceof DOMException && cause.name === "AbortError"
          ? `timed out after ${this.#timeoutMs}ms`
          : "network error";
      throw new TokenError(`Could not reach the identity provider (${reason}).`, false);
    } finally {
      clearTimeout(timer);
    }

    if (!response.ok) {
      // 400/401 here means the client id or secret is wrong, or the client is
      // disabled. Do NOT include the response body — it can echo the client id.
      const refused = response.status >= 400 && response.status < 500;
      throw new TokenError(
        `The identity provider rejected the app's credential (HTTP ${response.status}).`,
        refused,
      );
    }

    let payload: { access_token?: unknown; expires_in?: unknown };
    try {
      payload = (await response.json()) as typeof payload;
    } catch {
      throw new TokenError("The identity provider returned a non-JSON token response.", false);
    }

    if (typeof payload.access_token !== "string" || payload.access_token.length === 0) {
      throw new TokenError("The identity provider's response had no access_token.", false);
    }

    const expiresIn =
      typeof payload.expires_in === "number" && payload.expires_in > 0
        ? payload.expires_in
        : 300; // EAIP realm default, if the provider omits it.

    this.#cached = {
      value: payload.access_token,
      staleAt: Date.now() + expiresIn * 1000 - this.#skewMs,
    };
    return this.#cached.value;
  }
}
