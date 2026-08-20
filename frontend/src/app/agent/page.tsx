"use client";

import { useEffect, useRef, useState } from "react";
import { CornerDownLeft, Loader2, Sparkles } from "lucide-react";

import { AgentRunView } from "@/components/agent-run";
import { AppShell } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, type AgentResponse, api } from "@/lib/api";

interface Turn {
  question: string;
  response?: AgentResponse;
}

/**
 * The agent: questions that may need more than one source.
 *
 * Separate from /chat rather than replacing it, because they answer different
 * shapes of question and cost different amounts. Chat retrieves and generates;
 * this can also query the database, and pays for a planning call to decide.
 * Folding them together would hide that difference from whoever pays for it.
 */
export default function AgentPage() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [forceAgent, setForceAgent] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, pending]);

  async function send() {
    const trimmed = question.trim();
    if (!trimmed || pending) return;

    setTurns((t) => [...t, { question: trimmed }]);
    setQuestion("");
    setPending(true);
    setError(null);

    try {
      const response = await api.agent(trimmed, forceAgent);
      setTurns((t) =>
        t.map((turn, i) => (i === t.length - 1 ? { ...turn, response } : turn)),
      );
    } catch (e) {
      setError(
        e instanceof ApiError
          ? e.message
          : e instanceof Error
            ? e.message
            : "The request failed.",
      );
    } finally {
      setPending(false);
    }
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  }

  return (
    <AppShell>
      <div className="mx-auto flex h-full max-w-3xl flex-col px-6">
        <header className="py-5">
          <h1 className="text-base font-semibold tracking-tight">Agent</h1>
          <p className="text-muted-foreground text-xs">
            Questions that may need documents and the database together.
          </p>
        </header>

        <div className="flex-1 space-y-6 overflow-y-auto pb-4">
          {turns.length === 0 ? <EmptyState /> : null}

          {turns.map((turn, i) => (
            <div key={i} className="space-y-2.5">
              <div className="flex justify-end">
                <div className="bg-secondary/60 max-w-[75%] rounded-lg rounded-br-sm px-3.5 py-2.5">
                  <p className="text-sm leading-relaxed whitespace-pre-wrap">
                    {turn.question}
                  </p>
                </div>
              </div>
              {turn.response ? <AgentRunView response={turn.response} /> : null}
            </div>
          ))}

          {pending ? (
            <div className="text-muted-foreground flex items-center gap-2 text-sm">
              <Loader2 className="size-3.5 animate-spin" />
              Planning and calling tools…
            </div>
          ) : null}

          {error ? (
            <p role="alert" className="text-destructive text-sm">
              {error}
            </p>
          ) : null}

          <div ref={bottomRef} />
        </div>

        <div className="border-border/60 bg-card/40 mb-6 rounded-lg border p-2 backdrop-blur-xl">
          <Textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Ask something that needs more than one source…"
            aria-label="Question"
            className="max-h-40 min-h-[52px] resize-none border-0 bg-transparent text-sm shadow-none focus-visible:ring-0"
            disabled={pending}
          />
          <div className="flex items-center justify-between gap-3 px-1.5 pb-0.5">
            <div className="flex items-center gap-2">
              <input
                id="force"
                type="checkbox"
                checked={forceAgent}
                onChange={(e) => setForceAgent(e.target.checked)}
                className="accent-primary size-3.5"
              />
              <Label
                htmlFor="force"
                className="text-muted-foreground/70 text-[11px] font-normal"
                title="Skip routing and always plan, even for a simple question"
              >
                Force the agent
              </Label>
            </div>
            <Button
              size="sm"
              onClick={() => void send()}
              disabled={pending || !question.trim()}
            >
              <CornerDownLeft className="size-3.5" />
              Ask
            </Button>
          </div>
        </div>
      </div>
    </AppShell>
  );
}

function EmptyState() {
  return (
    <div className="text-muted-foreground space-y-4 py-8 text-sm">
      <p className="leading-relaxed">
        The agent chooses tools one step at a time — searching documents,
        querying the database, or both — and shows every call it made.
      </p>

      <div className="border-border/50 bg-card/30 space-y-2 rounded-md border p-3">
        <p className="text-foreground flex items-center gap-2 text-xs font-medium">
          <Sparkles className="size-3.5" />
          Try
        </p>
        <ul className="space-y-1.5 text-xs">
          <li className="text-muted-foreground/90">
            “How many orders were shipped and what does the handbook say about
            shipping?” — needs both sources
          </li>
          <li className="text-muted-foreground/90">
            “How long does a refund take?” — routes straight to retrieval, no
            planning call
          </li>
        </ul>
      </div>

      <p className="text-muted-foreground/70 text-xs leading-relaxed">
        A simple question is answered without the agent, because a planning call
        before a retrieval call doubles the cost of a question that never needed
        a decision. Tick <em>Force the agent</em> to see it plan anyway.
      </p>
    </div>
  );
}
