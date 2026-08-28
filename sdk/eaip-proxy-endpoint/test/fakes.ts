/**
 * A fake Keycloak token endpoint and a fake EAIP /mcp, as one `fetch`.
 *
 * Between them they let the proxy's token caching, refresh, retry, and
 * pass-through be tested without either real service.
 */

export interface FakesOptions {
  tokenUrl: string;
  mcpUrl: string;

  /** Keycloak refuses the credential (400). */
  tokenRefused?: boolean;
  /** Keycloak is unreachable. */
  tokenNetworkError?: boolean;
  /** Seconds each issued token is said to last. Default 300. */
  tokenTtlSeconds?: number;

  /** EAIP returns 401 for the first N /mcp calls, then succeeds. */
  eaipUnauthorizedFirst?: number;
  /** EAIP is unreachable. */
  eaipNetworkError?: boolean;
  /** The JSON-RPC result EAIP returns for a successful tools/call. */
  eaipResult?: unknown;
}

export interface Fakes {
  fetch: typeof fetch;
  /** How many times the token endpoint was hit. */
  tokenRequests: number;
  /** Every bearer token EAIP saw, in order. */
  eaipTokensSeen: string[];
  /** Every JSON-RPC body EAIP saw. */
  eaipBodies: unknown[];
}

export function makeFakes(options: FakesOptions): Fakes {
  const state: Fakes = {
    fetch: undefined as never,
    tokenRequests: 0,
    eaipTokensSeen: [],
    eaipBodies: [],
  };
  let issued = 0;
  let mcpCalls = 0;

  state.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();

    if (url === options.tokenUrl) {
      state.tokenRequests++;
      if (options.tokenNetworkError) throw new TypeError("fetch failed");
      if (options.tokenRefused) {
        return jsonResponse({ error: "invalid_client" }, 400);
      }
      issued++;
      return jsonResponse(
        {
          access_token: `token-${issued}`,
          expires_in: options.tokenTtlSeconds ?? 300,
          token_type: "Bearer",
        },
        200,
      );
    }

    if (url === options.mcpUrl) {
      if (options.eaipNetworkError) throw new TypeError("fetch failed");
      mcpCalls++;
      const auth = String(
        (init?.headers as Record<string, string> | undefined)?.Authorization ??
          (init?.headers as Record<string, string> | undefined)?.authorization ??
          "",
      );
      state.eaipTokensSeen.push(auth.replace(/^Bearer /, ""));
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      state.eaipBodies.push(body);

      if (options.eaipUnauthorizedFirst && mcpCalls <= options.eaipUnauthorizedFirst) {
        return jsonResponse(
          { jsonrpc: "2.0", id: body?.id ?? null, error: { code: -32001, message: "Unauthorized" } },
          401,
        );
      }

      return jsonResponse(
        {
          jsonrpc: "2.0",
          id: body?.id ?? null,
          result: options.eaipResult ?? {
            content: [{ type: "text", text: "ok" }],
            isError: false,
          },
        },
        200,
      );
    }

    return jsonResponse({ error: "unexpected url", url }, 404);
  }) as typeof fetch;

  return state;
}

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export const TOKEN_URL = "https://kc.test/realms/eaip/protocol/openid-connect/token";
export const MCP_URL = "https://eaip.test/mcp";

export function testConfig() {
  return {
    tokenUrl: TOKEN_URL,
    mcpUrl: MCP_URL,
    clientId: "eaip-mcp-test",
    clientSecret: "shhh",
  };
}
