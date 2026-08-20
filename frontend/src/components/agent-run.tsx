"use client";

import { useState } from "react";
import {
  AlertCircle,
  ChevronRight,
  Database,
  FileSearch,
  Globe,
  ShieldOff,
  Wrench,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { AgentResponse, AgentToolCall } from "@/lib/api";

/**
 * One agent run, with its reasoning visible.
 *
 * The tool calls are the substance. An agent answer assembled from two sources
 * is only checkable if you can see which sources, in what order, and what each
 * returned — the same argument as citations in chat, applied to a run that may
 * have consulted a database rather than a document.
 *
 * Denied calls are shown rather than filtered. A model requesting a tool it is
 * not authorized for is exactly what a successful prompt injection looks like,
 * and hiding it would remove the only place a person would notice.
 */
export function AgentRunView({ response }: { response: AgentResponse }) {
  const denied = response.tool_calls.filter((c) => c.denied).length;

  return (
    <div className="space-y-3">
      {response.answer ? (
        <div className="bg-card/60 border-border/60 rounded-lg border px-3.5 py-2.5">
          <p className="text-sm leading-relaxed whitespace-pre-wrap">
            {response.answer}
          </p>
        </div>
      ) : (
        <div className="bg-muted/40 border-border/60 flex gap-2.5 rounded-lg border px-3.5 py-2.5">
          <AlertCircle className="text-muted-foreground mt-0.5 size-4 shrink-0" />
          <div>
            <p className="text-sm leading-relaxed">
              {response.halted_reason ?? "The run ended without an answer."}
            </p>
            <p className="text-muted-foreground mt-1.5 text-xs">
              A run that stops is an outcome, not a failure — the limits exist so
              a question that has gone wrong does not keep spending.
            </p>
          </div>
        </div>
      )}

      {response.tool_calls.length > 0 ? (
        <ul className="space-y-1.5">
          {response.tool_calls.map((call, i) => (
            <ToolCallCard key={i} call={call} index={i + 1} />
          ))}
        </ul>
      ) : null}

      <div className="text-muted-foreground/70 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px]">
        {response.routed_directly ? (
          // Worth surfacing: it explains why a simple question cost a fifth as
          // much and shows no reasoning steps.
          <Badge
            variant="outline"
            className="h-4 px-1.5 font-mono text-[10px] font-normal"
            title="Answered without the agent — no planning call was made"
          >
            routed
          </Badge>
        ) : (
          <span>{response.steps} steps</span>
        )}
        <span>
          {response.tool_calls.length} tool
          {response.tool_calls.length === 1 ? "" : "s"}
        </span>
        {denied > 0 ? (
          <span className="text-destructive/80">
            {denied} denied
          </span>
        ) : null}
        <span>${response.cost_usd.toFixed(6)}</span>
        <Badge
          variant="outline"
          className="h-4 px-1.5 font-mono text-[10px] font-normal"
          title="Trace id — matches rows in trace_span"
        >
          {response.trace_id.slice(0, 8)}
        </Badge>
      </div>
    </div>
  );
}

function ToolCallCard({ call, index }: { call: AgentToolCall; index: number }) {
  const [open, setOpen] = useState(false);
  // A denial is not an ordinary failure. It means the model asked for something
  // it was not authorized to use, which is worth distinguishing visually.
  const isDenial = call.denied;

  return (
    <li
      className={cn(
        "rounded-md border",
        isDenial
          ? "border-destructive/40 bg-destructive/5"
          : call.error
            ? "border-border/60 bg-muted/30"
            : "border-border/50 bg-card/30",
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="hover:bg-accent/20 flex w-full items-center gap-2 rounded-md px-3 py-2 text-left transition-colors"
        aria-expanded={open}
      >
        <ChevronRight
          className={cn(
            "text-muted-foreground size-3.5 shrink-0 transition-transform",
            open && "rotate-90",
          )}
        />
        <span className="text-muted-foreground font-mono text-[11px]">{index}</span>
        <ToolIcon name={call.tool} denied={isDenial} />
        <span className="flex-1 truncate font-mono text-xs">{call.tool}</span>

        {isDenial ? (
          <Badge variant="destructive" className="h-5 px-1.5 text-[10px]">
            denied
          </Badge>
        ) : call.error ? (
          <Badge variant="outline" className="h-5 px-1.5 text-[10px]">
            failed
          </Badge>
        ) : null}

        <span className="text-muted-foreground font-mono text-[11px] tabular-nums">
          {Math.round(call.duration_ms)}ms
        </span>
      </button>

      {open ? (
        <div className="border-border/40 space-y-2 border-t px-3 py-2">
          {Object.keys(call.arguments).length > 0 ? (
            <div>
              <p className="text-muted-foreground/70 mb-1 text-[10px] uppercase tracking-wider">
                arguments
              </p>
              <pre className="text-foreground/80 overflow-x-auto font-mono text-[11px] whitespace-pre-wrap">
                {JSON.stringify(call.arguments, null, 2)}
              </pre>
            </div>
          ) : null}

          {call.error ? (
            <p className="text-destructive/90 text-[11px]">{call.error}</p>
          ) : (
            <div>
              <p className="text-muted-foreground/70 mb-1 text-[10px] uppercase tracking-wider">
                result
              </p>
              {/* The full result, not a preview. This is what the answer was
                  built from, and a truncated version cannot be checked. */}
              <pre className="text-foreground/80 max-h-64 overflow-auto font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
                {call.content}
              </pre>
            </div>
          )}
        </div>
      ) : null}
    </li>
  );
}

function ToolIcon({ name, denied }: { name: string; denied: boolean }) {
  const className = "size-3.5 shrink-0 text-muted-foreground";
  if (denied) return <ShieldOff className={className} />;
  if (name.includes("database")) return <Database className={className} />;
  if (name.includes("knowledge") || name.includes("search"))
    return <FileSearch className={className} />;
  if (name.includes("api")) return <Globe className={className} />;
  return <Wrench className={className} />;
}
