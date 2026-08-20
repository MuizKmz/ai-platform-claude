"use client";

import { useState } from "react";
import { ChevronRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { Span, Trace } from "@/lib/api";

/**
 * One request, as a timeline of the spans it produced.
 *
 * The bars are proportional to duration, which is the whole point: a request
 * that took 900ms tells you nothing until you can see that 850ms of it was the
 * model and 40ms was retrieval. A table of numbers makes you do that arithmetic
 * yourself, and people mostly do not.
 *
 * The roadmap put this before the agent deliberately — debugging a multi-step
 * agent without it is miserable, and by Phase 7 a trace will have five or six
 * spans rather than two.
 */
export function TraceRow({ trace }: { trace: Trace }) {
  const [open, setOpen] = useState(false);
  const failed = trace.status === "error";

  return (
    <li className="border-border/50 bg-card/30 rounded-md border">
      <button
        onClick={() => setOpen((v) => !v)}
        className="hover:bg-accent/30 flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors"
        aria-expanded={open}
      >
        <ChevronRight
          className={cn(
            "text-muted-foreground size-3.5 shrink-0 transition-transform",
            open && "rotate-90",
          )}
        />

        {/* The id is truncated because it is used for recognition here and
            copied in full from the detail below. */}
        <span className="text-muted-foreground font-mono text-[11px]">
          {trace.trace_id.slice(0, 8)}
        </span>

        <span className="flex-1 truncate text-xs">
          {trace.spans.map((s) => s.name).join(" → ")}
        </span>

        {failed ? (
          <Badge variant="destructive" className="h-5 px-1.5 text-[10px]">
            error
          </Badge>
        ) : null}

        <span className="font-mono text-xs tabular-nums">
          {formatMs(trace.total_duration_ms)}
        </span>

        <time
          dateTime={trace.started_at}
          className="text-muted-foreground/70 hidden font-mono text-[11px] sm:block"
        >
          {new Date(trace.started_at).toLocaleTimeString()}
        </time>
      </button>

      {open ? (
        <div className="border-border/40 space-y-2 border-t px-3 py-3">
          {trace.spans.map((span) => (
            <SpanBar
              key={span.id}
              span={span}
              totalMs={trace.total_duration_ms}
            />
          ))}

          <p className="text-muted-foreground/70 pt-1 font-mono text-[11px] break-all">
            {trace.trace_id}
          </p>
        </div>
      ) : null}
    </li>
  );
}

function SpanBar({ span, totalMs }: { span: Span; totalMs: number }) {
  // Floor at 2% so a 1ms span is still visible. A bar you cannot see is worse
  // than a slightly dishonest one, because it reads as a missing span.
  const width = totalMs > 0 ? Math.max((span.duration_ms / totalMs) * 100, 2) : 0;
  const failed = span.status === "error";

  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between gap-3">
        <span className="font-mono text-[11px]">{span.name}</span>
        <span className="text-muted-foreground font-mono text-[11px] tabular-nums">
          {formatMs(span.duration_ms)}
          {span.token_cost ? ` · ${span.token_cost} tok` : ""}
        </span>
      </div>

      <div className="bg-secondary/40 h-1.5 overflow-hidden rounded-full">
        <div
          className={cn("h-full rounded-full", failed ? "bg-destructive" : "bg-primary")}
          style={{ width: `${width}%` }}
        />
      </div>

      {Object.keys(span.attributes).length > 0 ? (
        // Attributes are counts and lengths only — never query text or chunk
        // content. A span table is a second copy of tenant data that nobody
        // thinks to review, so the backend keeps it deliberately thin.
        <p className="text-muted-foreground/60 mt-1 font-mono text-[10px]">
          {Object.entries(span.attributes)
            .map(([key, value]) => `${key}=${String(value)}`)
            .join("  ")}
        </p>
      ) : null}
    </div>
  );
}

function formatMs(ms: number): string {
  // Sub-second in ms, above that in seconds. "1284ms" is harder to judge at a
  // glance than "1.28s", and "0.04s" is harder than "42ms".
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(2)}s`;
}
