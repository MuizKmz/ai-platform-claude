/**
 * Three error classes, because a caller does three different things about them.
 *
 * The widget shows a different message for each: "ask your admin to finish
 * setup" is not "you're not allowed to see that" is not "the knowledge base
 * couldn't answer". Matching on error text is how that goes wrong on the next
 * wording change, so the class is the signal.
 */

/** Base for everything this client throws. */
export class EaipError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "EaipError";
  }
}

/**
 * The host app's proxy path is not reachable, or it reports it has no EAIP
 * credential configured. This is a deployment problem on the integrator's side,
 * not something an end user did — the widget should say "not configured yet",
 * not "error".
 *
 * Raised for: a network failure reaching `basePath`, a 404/502 from it, or a
 * `503` the proxy endpoint returns when its own `EAIP_MCP_CLIENT_SECRET` is
 * missing.
 */
export class EaipNotConfiguredError extends EaipError {
  constructor(message = "The EAIP connection is not configured on this app's backend yet.") {
    super(message);
    this.name = "EaipNotConfiguredError";
  }
}

/**
 * EAIP refused the call: a bad or expired service-account token, the wrong
 * audience, a tool the configured identity's labels do not permit, or a rate
 * limit / budget cap.
 *
 * `code` carries the JSON-RPC code when there was one (`RpcCode.UNAUTHORIZED`,
 * `RpcCode.RATE_LIMITED`, `RpcCode.OVER_BUDGET`) so a caller can tell "try
 * again later" from "this will never work".
 *
 * EAIP's auth errors are deliberately opaque — it does not say WHY a token was
 * refused, on purpose — so this message is generic by design, not by omission.
 */
export class EaipAuthError extends EaipError {
  readonly code: number | undefined;

  constructor(message = "EAIP rejected the request.", code?: number) {
    super(message);
    this.name = "EaipAuthError";
    this.code = code;
  }
}

/**
 * A tool ran and reported failure (`isError: true`) — a connector was
 * unreachable, a query found nothing, the knowledge base could not ground an
 * answer. The message is the tool's own and safe to show: EAIP scrubs
 * hostnames and upstream text before it leaves the tool.
 *
 * This is NOT thrown by `listTools`; it comes from `callTool` and from the
 * higher-level `ask`.
 */
export class EaipToolError extends EaipError {
  readonly toolName: string;

  constructor(toolName: string, message: string) {
    super(message);
    this.name = "EaipToolError";
    this.toolName = toolName;
  }
}
