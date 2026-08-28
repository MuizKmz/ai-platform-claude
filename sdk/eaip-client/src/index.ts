/**
 * @eaip/client — framework-agnostic access to EAIP's chat tools.
 *
 * Everything goes through the host app's own backend proxy. See
 * `docs/CHAT_WIDGET_SDK.md` for why the browser never talks to EAIP directly.
 */

export { EaipClient, type EaipClientOptions } from "./client.ts";
export {
  EaipError,
  EaipNotConfiguredError,
  EaipAuthError,
  EaipToolError,
} from "./errors.ts";
export {
  RpcCode,
  textOf,
  type McpToolSpec,
  type McpToolResult,
  type McpToolsListResult,
  type JsonRpcRequest,
  type JsonRpcResponse,
  type JsonRpcError,
  type JsonRpcId,
} from "./jsonrpc.ts";
