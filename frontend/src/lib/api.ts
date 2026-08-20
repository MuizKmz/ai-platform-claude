/**
 * The only place this application talks to the backend.
 *
 * The architecture review's constraint for this phase: "use Next.js as a pure
 * client of the FastAPI API — do not put business logic in route handlers, or
 * you will have two backends." Concentrating every call here is what makes that
 * enforceable rather than aspirational: a second place issuing fetches is
 * visible in review, and a route handler doing authorization is not.
 *
 * So this file translates HTTP into typed results and does nothing else. No
 * filtering, no permission checks, no deriving what a user may see. All of that
 * lives behind the API, where it is enforced by Row-Level Security and tested.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

/** Where the token lives in the browser.
 *
 * sessionStorage rather than localStorage: it is cleared when the tab closes,
 * which limits the window on a shared machine. Neither is safe against XSS —
 * an httpOnly cookie is, and is the Phase 8 hardening item. This is a
 * development console talking to a locally-issued token, and saying so plainly
 * is better than implying more than it delivers.
 */
const TOKEN_KEY = "eaip.token";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** True when re-authenticating is the fix. */
  get isAuthFailure(): boolean {
    return this.status === 401;
  }
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  window.sessionStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  window.sessionStorage.removeItem(TOKEN_KEY);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  });

  if (!response.ok) {
    // The backend returns a fixed, uninformative message for auth failures on
    // purpose — it must not be a debugging oracle. Surfacing it verbatim keeps
    // that property rather than inventing a friendlier explanation the server
    // deliberately withheld.
    const detail = await response
      .json()
      .then((body: { detail?: string }) => body.detail)
      .catch(() => null);
    throw new ApiError(response.status, detail ?? `Request failed (${response.status}).`);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// --- identity ---------------------------------------------------------------

export interface Principal {
  tenant_id: string;
  user_id: string;
  email: string;
  roles: string[];
  allowed_labels: string[];
}

export const api = {
  me: () => request<Principal>("/v1/me"),

  health: () =>
    request<{ status: string; db: string; redis: string }>("/health"),

  // --- knowledge ------------------------------------------------------------

  search: (query: string, limit = 5) =>
    request<SearchResponse>(
      `/v1/search?q=${encodeURIComponent(query)}&limit=${limit}`,
    ),

  chat: (question: string, conversationId?: string) =>
    request<ChatResponse>("/v1/chat", {
      method: "POST",
      body: JSON.stringify({
        question,
        conversation_id: conversationId ?? null,
      }),
    }),

  documents: (includeSuperseded = false) =>
    request<DocumentSummary[]>(
      `/v1/documents?include_superseded=${includeSuperseded}`,
    ),

  deleteDocument: (id: string) =>
    request<{ documents_deleted: number; chunks_deleted: number }>(
      `/v1/documents/${id}`,
      { method: "DELETE" },
    ),

  // --- jobs -----------------------------------------------------------------

  jobs: () => request<IngestJob[]>("/v1/jobs"),

  job: (id: string) => request<IngestJob>(`/v1/jobs/${id}`),

  startIngest: (directory: string, labels: string[]) =>
    request<IngestJob>("/v1/jobs/ingest", {
      method: "POST",
      body: JSON.stringify({ directory, labels }),
    }),

  // --- agent ----------------------------------------------------------------

  /** Ask a question that may need more than one source.
   *
   * The backend routes simple questions straight to grounded generation, so
   * this is not more expensive than /v1/chat for a single-source question —
   * `routed_directly` in the response says which path was taken.
   */
  agent: (question: string, forceAgent = false) =>
    request<AgentResponse>("/v1/agent", {
      method: "POST",
      body: JSON.stringify({ question, force_agent: forceAgent }),
    }),

  // --- observability --------------------------------------------------------
  // Admin-only. Both endpoints return records ABOUT users rather than for
  // them, and the API enforces the role — this client does not pre-check it.

  traces: (limit = 25) => request<Trace[]>(`/v1/traces?limit=${limit}`),

  audit: (limit = 50, deniedOnly = false) =>
    request<AuditEntry[]>(`/v1/audit?limit=${limit}&denied_only=${deniedOnly}`),
};

// --- response shapes --------------------------------------------------------
// Mirror the Pydantic models in backend/app/api/v1/. Kept as hand-written types
// rather than generated from OpenAPI: the surface is small, and a generator is
// another build step to keep working for little gain at this size.

export interface SearchResult {
  chunk_id: string;
  document_id: string;
  document_title: string;
  content: string;
  ordinal: number;
  score: number;
}

export interface SearchResponse {
  query: string;
  results: SearchResult[];
  filtered_by: { tenant_id: string; labels: string[] };
  trace_id: string;
}

export interface Citation {
  index: number;
  chunk_id: string;
  document_id: string;
  document_title: string;
  /** The passage the claim came from. Returned inline so checking an answer
   *  does not require a second request. */
  content: string;
}

export interface ChatResponse {
  answer: string;
  citations: Citation[];
  refused: boolean;
  conversation_id: string;
  trace_id: string;
  usage: {
    prompt_tokens: number;
    completion_tokens: number;
    cost_usd: number;
    model: string;
  };
}

export interface DocumentSummary {
  id: string;
  title: string;
  source_path: string;
  labels: string[];
  version: number;
  chunk_count: number;
  superseded_at: string | null;
  created_at: string;
}

export interface AgentToolCall {
  tool: string;
  arguments: Record<string, unknown>;
  /** What the tool returned. Shown for the same reason citations are: an agent
   *  answer is only checkable if you can see what it was built from. */
  content: string;
  error: string | null;
  duration_ms: number;
  /** True when the platform refused the call. Sent as its own field so the UI
   *  never has to detect an authorization failure by matching error text. */
  denied: boolean;
  /** True when this repeated an earlier identical call and was served from the
   *  first result instead of being re-run. */
  cached: boolean;
}

export interface AgentResponse {
  question: string;
  answer: string | null;
  /** Set when a run ended without an answer — a limit, a failure, a refusal.
   *  Distinct from `answer` so the UI can tell "here it is" from "here is why
   *  there isn't one". */
  halted_reason: string | null;
  tool_calls: AgentToolCall[];
  steps: number;
  cost_usd: number;
  trace_id: string;
  /** True when the question was answered without the agent. */
  routed_directly: boolean;
}

export interface Span {
  id: string;
  trace_id: string;
  span_id: string;
  name: string;
  duration_ms: number;
  status: string;
  token_cost: number | null;
  attributes: Record<string, unknown>;
  created_at: string;
}

export interface Trace {
  trace_id: string;
  started_at: string;
  total_duration_ms: number;
  status: string;
  span_count: number;
  spans: Span[];
}

export interface AuditEntry {
  id: string;
  user_email: string;
  connector_id: string;
  sql: string;
  question: string | null;
  allowed: boolean;
  denial_reason: string | null;
  row_count: number | null;
  duration_ms: number | null;
  trace_id: string | null;
  created_at: string;
}

export interface IngestJob {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  source_path: string;
  labels: string[];
  files_total: number;
  files_done: number;
  documents_created: number;
  chunks_created: number;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}
