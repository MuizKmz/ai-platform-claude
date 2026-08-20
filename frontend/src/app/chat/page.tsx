"use client";

import { useEffect, useRef, useState } from "react";
import { CornerDownLeft, Loader2 } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { ChatMessage, type Message } from "@/components/chat-message";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";

/**
 * Ask questions of the documents you are permitted to see.
 *
 * Two things this page deliberately does not do:
 *
 *   It does not filter, rank, or decide what you may see. Every one of those
 *   is enforced behind the API by Row-Level Security and the label predicates
 *   in the retrieval query. A frontend that reimplemented any of it would be a
 *   second, weaker copy of the rules.
 *
 *   It does not hide refusals. When the model says it has nothing to answer
 *   with, that is shown as the answer, because a blank response would read as
 *   a bug and an invented one would be worse than either.
 */
export default function ChatPage() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | undefined>();
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, pending]);

  async function send() {
    const trimmed = question.trim();
    if (!trimmed || pending) return;

    setMessages((m) => [...m, { role: "user", content: trimmed }]);
    setQuestion("");
    setPending(true);
    setError(null);

    try {
      const response = await api.chat(trimmed, conversationId);
      setConversationId(response.conversation_id);
      setMessages((m) => [
        ...m,
        { role: "assistant", content: response.answer, response },
      ]);
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
    // Enter sends, Shift+Enter breaks a line. The convention people already
    // have from every other chat interface.
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  }

  return (
    <AppShell>
      <div className="mx-auto flex h-dvh max-w-3xl flex-col px-6">
        <header className="py-5">
          <h1 className="text-base font-semibold tracking-tight">Chat</h1>
          <p className="text-muted-foreground text-xs">
            Answers come only from documents your labels permit.
          </p>
        </header>

        <div className="flex-1 space-y-5 overflow-y-auto pb-4">
          {messages.length === 0 ? <EmptyState /> : null}

          {messages.map((message, i) => (
            <ChatMessage key={i} message={message} />
          ))}

          {pending ? (
            <div className="text-muted-foreground flex items-center gap-2 text-sm">
              <Loader2 className="size-3.5 animate-spin" />
              Retrieving and generating…
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
            placeholder="Ask about your documents…"
            aria-label="Question"
            className="max-h-40 min-h-[52px] resize-none border-0 bg-transparent text-sm shadow-none focus-visible:ring-0"
            disabled={pending}
          />
          <div className="flex items-center justify-between px-1.5 pb-0.5">
            <span className="text-muted-foreground/60 text-[11px]">
              Enter to send · Shift+Enter for a new line
            </span>
            <Button
              size="sm"
              onClick={() => void send()}
              disabled={pending || !question.trim()}
            >
              <CornerDownLeft className="size-3.5" />
              Send
            </Button>
          </div>
        </div>
      </div>
    </AppShell>
  );
}

function EmptyState() {
  return (
    <div className="text-muted-foreground py-10 text-sm">
      <p className="mb-3">
        Ask a question and the platform will retrieve relevant passages, generate
        an answer from them, and cite what it used.
      </p>
      <p className="text-muted-foreground/70 text-xs leading-relaxed">
        Every citation is verified server-side against the chunks actually
        retrieved, so a numbered reference always names a real source you are
        permitted to see. If nothing relevant is found, you will get a refusal
        rather than a guess.
      </p>
    </div>
  );
}
