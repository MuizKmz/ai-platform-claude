/**
 * The four things the proxy needs, and where they come from.
 *
 * All four are the integrator's, set in the integrator's own `.env` — none of
 * them are EAIP's to hand out. `EAIP_MCP_CLIENT_SECRET` is the one that matters:
 * it is what lets this backend act as the service-account identity an EAIP admin
 * scoped in Keycloak (see `infra/keycloak/MCP-SETUP.md`).
 */

export interface EaipProxyConfig {
  /**
   * Keycloak's token endpoint for the realm — the full URL, e.g.
   * `https://aiplatform.clbgroups.com/realms/eaip/protocol/openid-connect/token`.
   * From `EAIP_MCP_TOKEN_URL`.
   */
  tokenUrl: string;

  /** The Keycloak client id, e.g. `eaip-mcp` or `eaip-mcp-wms`. From `EAIP_MCP_CLIENT_ID`. */
  clientId: string;

  /**
   * The client secret from Keycloak's Credentials tab. From
   * `EAIP_MCP_CLIENT_SECRET`. Treat like a password — it is the whole
   * server-side boundary. Never logged, never returned in a response.
   */
  clientSecret: string;

  /**
   * EAIP's MCP endpoint — the full URL, e.g.
   * `https://aiplatform.clbgroups.com/mcp` once that path is routed, or an
   * internal address the host reaches server-to-server. From `EAIP_MCP_URL`.
   *
   * Note: in the current production deployment `/mcp` is NOT publicly routed
   * (see `docs/CHAT_WIDGET_SDK.md`). This backend reaches it over whatever
   * server-to-server path the integrator has — a private network address, an
   * SSH tunnel, or a proxy rule they add on their own infrastructure. The point
   * of this whole design is that the *browser* never needs it; this backend
   * does, once.
   */
  mcpUrl: string;

  /**
   * Optional. Seconds before a token's real expiry to treat it as stale and
   * refresh early. Default 30. EAIP's access tokens live 300s.
   */
  refreshSkewSeconds?: number;

  /** Optional. Per-upstream-call timeout in ms. Default 30000. */
  timeoutMs?: number;
}

/**
 * Build a config from `process.env`, or return the list of what's missing.
 *
 * Returning the missing names rather than throwing lets the caller decide: a
 * dev server might log and mount a "not configured" handler (so the widget
 * shows a clean state), while CI might hard-fail.
 */
export function configFromEnv(
  env: Record<string, string | undefined> = process.env,
): { config: EaipProxyConfig } | { missing: string[] } {
  const required = {
    tokenUrl: env.EAIP_MCP_TOKEN_URL,
    clientId: env.EAIP_MCP_CLIENT_ID,
    clientSecret: env.EAIP_MCP_CLIENT_SECRET,
    mcpUrl: env.EAIP_MCP_URL,
  };

  const missing = Object.entries(required)
    .filter(([, value]) => !value)
    .map(([key]) => envNameFor(key));

  if (missing.length > 0) return { missing };

  const config: EaipProxyConfig = {
    tokenUrl: required.tokenUrl!,
    clientId: required.clientId!,
    clientSecret: required.clientSecret!,
    mcpUrl: required.mcpUrl!,
  };
  if (env.EAIP_MCP_REFRESH_SKEW_SECONDS) {
    config.refreshSkewSeconds = Number(env.EAIP_MCP_REFRESH_SKEW_SECONDS);
  }
  if (env.EAIP_MCP_TIMEOUT_MS) {
    config.timeoutMs = Number(env.EAIP_MCP_TIMEOUT_MS);
  }
  return { config };
}

function envNameFor(key: string): string {
  return (
    {
      tokenUrl: "EAIP_MCP_TOKEN_URL",
      clientId: "EAIP_MCP_CLIENT_ID",
      clientSecret: "EAIP_MCP_CLIENT_SECRET",
      mcpUrl: "EAIP_MCP_URL",
    }[key] ?? key
  );
}
