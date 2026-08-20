"use client";

import { useState } from "react";
import { AlertCircle, FileText } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { Citation, ChatResponse } from "@/lib/api";

export interface Message {
  role: "user" | "assistant";
  content: string;
  response?: ChatResponse;
}

/**
 * One turn in the conversation.
 *
 * The citation rendering is the point of this component. The backend verifies
 * every [n] against the chunks actually retrieved for that request and strips
 * the ones it cannot resolve, so a marker that survives to here is guaranteed
 * to name a real chunk in this tenant. That guarantee is worth surfacing: a
 * citation the reader can open is evidence, and one they cannot is decoration.
 *
 * Published text-to-SQL and RAG accuracy both sit well short of 100%, and a
 * wrong answer does not announce itself. Making the sources one click away is
 * what lets someone catch it.
 */
export function ChatMessage({ message }: { message: Message }) {
  // Which citation the reader has focused, if any. Lifted to this level
  // because clicking [1] in the answer has to open the card below it — an
  // anchor jump alone did nothing visible when the card was already on screen,
  // which read as a broken link.
  const [focused, setFocused] = useState<number | null>(null);

  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="bg-secondary/60 max-w-[75%] rounded-lg rounded-br-sm px-3.5 py-2.5">
          <p className="text-sm leading-relaxed whitespace-pre-wrap">
            {message.content}
          </p>
        </div>
      </div>
    );
  }

  const response = message.response;
  const refused = response?.refused ?? false;

  return (
    <div className="space-y-2.5">
      <div
        className={cn(
          "max-w-[85%] rounded-lg rounded-bl-sm px-3.5 py-2.5",
          // A refusal is styled as information, not as an error. "I don't have
          // enough information" is a correct answer when the sources are
          // silent, and dressing it in red would train people to distrust the
          // one behaviour that keeps the system honest.
          refused
            ? "bg-muted/40 border-border/60 border"
            : "bg-card/60 border-border/60 border",
        )}
      >
        {refused ? (
          <div className="flex gap-2.5">
            <AlertCircle className="text-muted-foreground mt-0.5 size-4 shrink-0" />
            <div>
              <p className="text-sm leading-relaxed">{message.content}</p>
              <p className="text-muted-foreground mt-1.5 text-xs">
                Nothing in the documents you can see answers this. That is a
                refusal, not a failure.
              </p>
            </div>
          </div>
        ) : (
          <p className="text-sm leading-relaxed whitespace-pre-wrap">
            <AnnotatedAnswer
              text={message.content}
              citations={response?.citations ?? []}
              onSelect={setFocused}
            />
          </p>
        )}
      </div>

      {response && response.citations.length > 0 ? (
        <CitationList
          citations={response.citations}
          focused={focused}
          onToggle={(index) => setFocused((f) => (f === index ? null : index))}
        />
      ) : null}

      {response ? <ResponseMeta response={response} /> : null}
    </div>
  );
}

/**
 * Render [1] markers as interactive references.
 *
 * Markers are matched against the verified citation list rather than styled on
 * sight: if the backend stripped one, it should read as plain text here too.
 */
function AnnotatedAnswer({
  text,
  citations,
  onSelect,
}: {
  text: string;
  citations: Citation[];
  onSelect: (index: number) => void;
}) {
  const byIndex = new Map(citations.map((c) => [c.index, c]));
  const parts = text.split(/(\[\d+\])/g);

  return (
    <>
      {parts.map((part, i) => {
        const match = /^\[(\d+)\]$/.exec(part);
        const citation = match ? byIndex.get(Number(match[1])) : undefined;

        if (!citation) return <span key={i}>{part}</span>;

        return (
          // A button rather than an anchor. The anchor jumped to an element
          // already in view, so nothing moved and nothing opened — the click
          // registered and looked ignored.
          <button
            key={i}
            type="button"
            onClick={() => onSelect(citation.index)}
            className="text-primary hover:bg-primary/20 bg-primary/10 mx-0.5 rounded px-1.5 font-mono text-[11px] transition-colors"
            title={`Show source: ${citation.document_title}`}
          >
            {citation.index}
          </button>
        );
      })}
    </>
  );
}

function CitationList({
  citations,
  focused,
  onToggle,
}: {
  citations: Citation[];
  focused: number | null;
  onToggle: (index: number) => void;
}) {
  return (
    <ul className="space-y-1.5">
      {citations.map((citation) => (
        <CitationCard
          key={citation.chunk_id}
          citation={citation}
          open={focused === citation.index}
          onToggle={() => onToggle(citation.index)}
        />
      ))}
    </ul>
  );
}

function CitationCard({
  citation,
  open,
  onToggle,
}: {
  citation: Citation;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <li
      id={`citation-${citation.index}`}
      className={cn(
        "rounded-md border px-3 py-2 transition-colors",
        // The focused card is visibly picked out, which is the feedback the
        // anchor version never gave.
        open
          ? "border-primary/40 bg-primary/5"
          : "border-border/50 bg-card/30",
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2 text-left"
        aria-expanded={open}
      >
        <span className="text-primary font-mono text-[11px]">
          {citation.index}
        </span>
        <FileText className="text-muted-foreground size-3.5 shrink-0" />
        <span className="flex-1 truncate text-xs">
          {citation.document_title}
        </span>
      </button>

      {open ? (
        <div className="border-border/40 mt-2 space-y-2 border-t pt-2">
          {/* The passage itself, which is what makes a citation evidence
              rather than a label. A reader deciding whether to trust an answer
              can see the text it was drawn from. */}
          <p className="text-foreground/85 text-xs leading-relaxed whitespace-pre-wrap">
            {citation.content}
          </p>
          {/* The id stays, below and dimmed: it is what you would paste into a
              query to check this by hand, but it is not the thing you read. */}
          <p className="text-muted-foreground/60 font-mono text-[10px] break-all">
            chunk {citation.chunk_id}
          </p>
        </div>
      ) : null}
    </li>
  );
}

function ResponseMeta({ response }: { response: ChatResponse }) {
  const cost = Number(response.usage.cost_usd ?? 0);

  return (
    <div className="text-muted-foreground/70 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px]">
      <span>{String(response.usage.model)}</span>
      <span>
        {String(response.usage.prompt_tokens)}+
        {String(response.usage.completion_tokens)} tok
      </span>
      {/* Six decimals because a single answer costs a fraction of a cent, and
          rounding to two would show every request as $0.00. */}
      <span>${cost.toFixed(6)}</span>
      <Badge
        variant="outline"
        className="h-4 px-1.5 font-mono text-[10px] font-normal"
        title="Trace id — matches a row in trace_span"
      >
        {response.trace_id.slice(0, 8)}
      </Badge>
    </div>
  );
}
