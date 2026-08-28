/**
 * The hook the widget is built on, exported so an integrator can build their
 * own UI on top of the same conversation logic.
 *
 * It owns: the message list, the pending state, the "not configured" /
 * "unauthorized" / "tool error" distinction, and the one call to the client.
 *
 * It deliberately does NOT own retrieval, ranking, or anything about what the
 * answer contains — that is EAIP's, behind the tool. The hook sends a question
 * and renders what comes back, including a refusal.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  EaipAuthError,
  EaipClient,
  EaipNotConfiguredError,
  EaipToolError,
  type EaipClientOptions,
} from "@eaip/client";

export interface EaipMessage {
  role: "user" | "assistant";
  content: string;
  /** Set on an assistant message when the tool reported failure — the widget
   *  styles it as a soft error rather than a normal answer. */
  isError?: boolean;
}

export type EaipStatus =
  | "checking" // startSession in flight
  | "ready"
  | "not-configured" // the host app's proxy is not set up
  | "unauthorized"; // the app's EAIP credential was refused

export interface UseEaipChatOptions extends EaipClientOptions {
  /**
   * Which EAIP tool a plain message calls, and the name of the argument the
   * message text goes in. Default: `search_knowledge` / `query`, which is the
   * grounded-generation search every EAIP tenant has.
   *
   * Point it at a `query_*` tool (with `argument: "question"`) if this
   * integration should hit a database connector instead — but note MCP refuses
   * the free-text SQL path (`infra/keycloak/MCP-SETUP.md`), so only templated
   * questions work there.
   */
  tool?: string;
  argument?: string;

  /** Called once per successful or failed exchange, for the host app's own
   *  analytics. Never receives a token or anything sensitive. */
  onExchange?: (info: { question: string; ok: boolean }) => void;
}

export interface UseEaipChat {
  messages: EaipMessage[];
  status: EaipStatus;
  pending: boolean;
  /** A transient error from the *last* send (network, rate limit). Distinct
   *  from `status`, which is about whether the connection works at all. */
  error: string | null;
  send: (text: string) => Promise<void>;
  /** Clear the transcript. Does not re-check the connection. */
  reset: () => void;
}

export function useEaipChat(options: UseEaipChatOptions = {}): UseEaipChat {
  const {
    tool = "search_knowledge",
    argument = "query",
    onExchange,
    ...clientOptions
  } = options;

  // Rebuild the client only when a connection-relevant option changes.
  const client = useMemo(
    () => new EaipClient(clientOptions),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [clientOptions.basePath, clientOptions.timeoutMs, clientOptions.fetch],
  );

  const [messages, setMessages] = useState<EaipMessage[]>([]);
  const [status, setStatus] = useState<EaipStatus>("checking");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Guard against setting state after unmount when startSession resolves late.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    setStatus("checking");
    client
      .startSession()
      .then(() => {
        if (alive.current) setStatus("ready");
      })
      .catch((err: unknown) => {
        if (!alive.current) return;
        if (err instanceof EaipAuthError) setStatus("unauthorized");
        else setStatus("not-configured");
      });
  }, [client]);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || pending) return;

      setMessages((m) => [...m, { role: "user", content: trimmed }]);
      setPending(true);
      setError(null);

      try {
        const answer = await client.ask(tool, { [argument]: trimmed });
        if (!alive.current) return;
        setMessages((m) => [...m, { role: "assistant", content: answer }]);
        onExchange?.({ question: trimmed, ok: true });
      } catch (err: unknown) {
        if (!alive.current) return;
        if (err instanceof EaipToolError) {
          // A tool that ran and reported failure — a dead connector, nothing
          // found. Show it as an assistant message, styled as an error, the
          // same way the console shows a refusal rather than hiding it.
          setMessages((m) => [
            ...m,
            { role: "assistant", content: err.message, isError: true },
          ]);
          onExchange?.({ question: trimmed, ok: false });
        } else if (err instanceof EaipNotConfiguredError) {
          setStatus("not-configured");
          onExchange?.({ question: trimmed, ok: false });
        } else if (err instanceof EaipAuthError) {
          // Rate limit / budget / a token that went bad mid-session. Not a
          // permanent "unauthorized" state — a transient error on this send.
          setError(err.message);
          onExchange?.({ question: trimmed, ok: false });
        } else {
          setError(err instanceof Error ? err.message : "The request failed.");
          onExchange?.({ question: trimmed, ok: false });
        }
      } finally {
        if (alive.current) setPending(false);
      }
    },
    [client, tool, argument, pending, onExchange],
  );

  const reset = useCallback(() => {
    setMessages([]);
    setError(null);
  }, []);

  return { messages, status, pending, error, send, reset };
}
