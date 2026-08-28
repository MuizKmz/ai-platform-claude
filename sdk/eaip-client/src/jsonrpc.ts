/**
 * The JSON-RPC 2.0 shapes EAIP's MCP server speaks, and nothing more.
 *
 * These mirror `backend/app/mcp/server.py` and `backend/app/mcp/tool_bridge.py`.
 * They are hand-written rather than generated: the surface is four methods and
 * the server pins its own protocol version, so a generator is a build step to
 * keep working for no gain at this size.
 */

/** A request id. The client always sends a number; the server echoes it back. */
export type JsonRpcId = number | string | null;

export interface JsonRpcRequest {
  jsonrpc: "2.0";
  id: JsonRpcId;
  method: string;
  params?: Record<string, unknown>;
}

export interface JsonRpcError {
  code: number;
  message: string;
  data?: unknown;
}

export interface JsonRpcResponse<T = unknown> {
  jsonrpc: "2.0";
  id: JsonRpcId;
  result?: T;
  error?: JsonRpcError;
}

/**
 * One tool, as MCP describes it. `inputSchema` is always an object of
 * all-string properties — EAIP's `ToolSpec.parameters` is a flat name→
 * description map and `spec_to_mcp` converts it verbatim, so `required` is
 * always `[]` and every property is `{ type: "string" }`. A tool that needs an
 * argument validates it and returns a readable error rather than a
 * protocol-level rejection.
 */
export interface McpToolSpec {
  name: string;
  description: string;
  inputSchema: {
    type: "object";
    properties: Record<string, { type: "string"; description?: string }>;
    required: string[];
  };
}

export interface McpToolsListResult {
  tools: McpToolSpec[];
}

/**
 * A tool result. `content` is an array of parts; EAIP only ever returns a
 * single `{ type: "text" }` part (`result_to_mcp`), but the array shape is the
 * spec's and worth preserving so a future part type does not break parsing.
 *
 * `isError: true` is a tool that FAILED, not a protocol error — the message is
 * in `content` and a caller should show it. A dead connector produces
 * `isError: true` with "I could not reach X", not an exception.
 */
export interface McpToolResult {
  content: Array<{ type: string; text?: string }>;
  isError: boolean;
}

/** JSON-RPC error codes EAIP's server uses. The spec owns all but the last band. */
export const RpcCode = {
  PARSE_ERROR: -32700,
  INVALID_REQUEST: -32600,
  METHOD_NOT_FOUND: -32601,
  INVALID_PARAMS: -32602,
  INTERNAL_ERROR: -32603,
  /** EAIP's own band. */
  UNAUTHORIZED: -32001,
  RATE_LIMITED: -32002,
  OVER_BUDGET: -32003,
} as const;

/** Pull the plain text out of an MCP tool result's content parts. */
export function textOf(result: McpToolResult): string {
  return result.content
    .filter((part) => part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n");
}
