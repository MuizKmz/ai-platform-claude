/**
 * @eaip/proxy-endpoint — the host-backend half of the EAIP chat widget.
 *
 * The package root is framework-agnostic. For Express, import from
 * `@eaip/proxy-endpoint/express`.
 *
 * See `sdk/SETUP.md` and `docs/CHAT_WIDGET_SDK.md`.
 */

export { configFromEnv, type EaipProxyConfig } from "./config.ts";
export {
  createEaipProxy,
  type EaipProxy,
  type ProxyRequest,
  type ProxyResponse,
} from "./handler.ts";
export { TokenCache, TokenError } from "./token.ts";
