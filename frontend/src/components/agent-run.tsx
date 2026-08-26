"use client";

import { useState } from "react";
import {
  AlertCircle,
  ChevronRight,
  Database,
  FileSearch,
  Globe,
  HelpCircle,
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
  // The server draws this distinction, because the same decision also governs
  // `denied_tool_calls` in the run record. Re-deriving it here from the offered
  // list would be a second implementation, free to disagree with the first.
  const denied = response.tool_calls.filter(
    (c) => c.denied && !c.unknown_tool,
  ).length;
  const invented = response.tool_calls.filter((c) => c.unknown_tool).length;
  const repeats = response.tool_calls.filter((c) => c.cached).length;
  const directResult = directResultSummary(response);

  return (
    <div className="space-y-3">
      {directResult ? (
        <DirectResultCard result={directResult} />
      ) : response.answer ? (
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
        <details className="group">
          <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs">
            View source and SQL details
          </summary>
          <ul className="mt-2 space-y-1.5">
            {response.tool_calls.map((call, i) => (
              <ToolCallCard key={i} call={call} index={i + 1} />
            ))}
          </ul>
        </details>
      ) : null}

      <div className="text-muted-foreground/70 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px]">
        {response.routed_directly ? (
          // Worth surfacing: it explains why a simple question cost a fifth as
          // much and shows no reasoning steps.
          <Badge
            variant="outline"
            className="h-4 px-1.5 font-mono text-[10px] font-normal"
            title="Answered through the direct path — no planning call was made"
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
        {invented > 0 ? (
          <span title="The model requested a tool that does not exist">
            {invented} unknown
          </span>
        ) : null}
        {repeats > 0 ? <span>{repeats} repeat{repeats === 1 ? "" : "s"}</span> : null}
        <span>${response.cost_usd.toFixed(6)}</span>
        {response.available_tools.length > 0 ? (
          // The tools this caller actually held. An agent with one tool cannot
          // answer a two-source question however well it plans, and without
          // this the shortfall looks like a reasoning failure.
          <span title="Tools available to you for this run">
            {response.available_tools.join(" · ")}
          </span>
        ) : null}
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

interface DirectResult {
  label: string;
  value: string;
  detail: string;
  rows?: string[];
}

/** Every data line the tool returned, not only the first. */
function parseRows(content: string): { columns: string[]; rows: string[][] } | null {
  const match = /Columns:\s*([^\n]+)\n([\s\S]*)$/.exec(content);
  if (!match) return null;
  const columns = match[1].split("|").map((value) => value.trim());
  const rows = match[2]
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => line.split("|").map((cell) => cell.trim()))
    .filter((cells) => cells.length === columns.length);
  return rows.length ? { columns, rows } : null;
}

function directResultSummary(response: AgentResponse): DirectResult | null {
  if (!response.routed_directly || response.tool_calls.length !== 1) return null;
  const call = response.tool_calls[0];
  if (call.error) return null;
  const parsed = parseRows(call.content);
  if (!parsed) return null;
  const { columns, rows } = parsed;
  const values = rows[0];

  // Several rows is a LIST, and rendering only the first as a hero number is
  // how "which ones are offline?" answered "Device-001" when five were down.
  // A wrong answer that looks like a confident one.
  if (rows.length > 1) {
    const nameIndex = columns.indexOf("device_name");
    const labelIndex = nameIndex >= 0 ? nameIndex : 0;
    return {
      label: columns[labelIndex].replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase()),
      value: String(rows.length),
      detail: `${rows.length} results · Live data`,
      rows: rows.map((row) => {
        const primary = row[labelIndex];
        const extras = row.filter((_, i) => i !== labelIndex).join(" · ");
        return extras ? `${primary} — ${extras}` : primary;
      }),
    };
  }

  if (columns.length !== values.length || values.length === 0) return null;
  const firstColumn = columns[0].replaceAll("_", " ");
  const firstValue = values[0];

  // An aggregate over no rows is NULL, and "None°C" reads as a reading of
  // None rather than an absence of readings. Fall through to the plain answer
  // text, which says so in words.
  if (values.every((value) => ["none", "null", ""].includes(value.toLowerCase()))) {
    return null;
  }
  const deviceId = values[columns.indexOf("device_id")];
  const deviceName = values[columns.indexOf("device_name")];
  const status = values[columns.indexOf("status")];
  if (deviceId && deviceName && status) {
    return {
      label: `${status.charAt(0).toUpperCase()}${status.slice(1)} Device`,
      value: deviceName,
      detail: `Device ID ${deviceId} \u00b7 Live status`,
    };
  }
  const numeric = Number(firstValue);
  const value = Number.isFinite(numeric) && firstValue.includes(".") ? numeric.toFixed(2) : firstValue;
  // Which metric this is, read from the SQL rather than assumed. Previously
  // only `temperature` was recognised and only °C could be shown, so humidity
  // or voltage rendered as a bare number under a label saying nothing about
  // what had been measured.
  const metric = /metric\s*=\s*'([a-z0-9_]+)'/i.exec(call.content)?.[1]?.toLowerCase();
  const unit = metric ? METRIC_UNITS[metric] : undefined;
  const label = firstColumn.replace(/\b\w/g, (letter) => letter.toUpperCase());
  const detail = metric
    ? `${metric.replaceAll("_", " ")} · ${measurementWindow(response.question)}`
    : response.question.toLowerCase().includes("online")
      ? "Live device status"
      : "Live data";
  return { label, value: unit ? `${value}${unit}` : value, detail };
}

/**
 * Units for the metrics this platform has met. A metric with no entry shows
 * its bare value: a guessed unit is worse than none, because it reads as a
 * measured fact rather than an assumption.
 */
const METRIC_UNITS: Record<string, string> = {
  temperature: "°C",
  humidity: "%",
  voltage: "V",
  bus_voltage: "V",
  pressure: " hPa",
  energy: " kWh",
  co2: " ppm",
};

function measurementWindow(question: string): string {
  const relative = /\b(?:last|past)\s+(\d+)\s+(hours?|days?)\b/i.exec(question);
  if (relative) return `last ${relative[1]} ${relative[2].toLowerCase()}`;
  if (/\btoday\b/i.test(question)) return "today";
  return "all available readings";
}

function DirectResultCard({ result }: { result: DirectResult }) {
  return (
    <div className="border-primary/25 bg-card/70 rounded-xl border px-4 py-3.5">
      <p className="text-muted-foreground text-xs">{result.label}</p>
      {result.rows ? (
        <ul className="mt-1.5 space-y-1">
          {result.rows.slice(0, 25).map((row, i) => (
            <li key={i} className="text-sm leading-relaxed">
              {row}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-1 text-3xl font-semibold tracking-tight tabular-nums">{result.value}</p>
      )}
      <p className="text-muted-foreground mt-1.5 text-xs">{result.detail}</p>
    </div>
  );
}

function ToolCallCard({ call, index }: { call: AgentToolCall; index: number }) {
  const [open, setOpen] = useState(false);
  // Two different events wear the same error. A tool the caller may not use is
  // a real refusal; a tool that exists nowhere is the model inventing a name.
  // Only the first says anything about authorization, and showing both as
  // "denied" makes an ordinary hallucination look like an escalation attempt.
  const isUnknown = call.unknown_tool;
  const isDenial = call.denied && !isUnknown;

  return (
    <li
      className={cn(
        "rounded-md border",
        isDenial
          ? "border-destructive/40 bg-destructive/5"
          : call.error || isUnknown
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
        <ToolIcon name={call.tool} denied={isDenial} unknown={isUnknown} />
        <span className="flex-1 truncate font-mono text-xs">{call.tool}</span>

        {isDenial ? (
          <Badge
            variant="destructive"
            className="h-5 px-1.5 text-[10px]"
            title="This tool exists but you are not authorized to use it"
          >
            denied
          </Badge>
        ) : isUnknown ? (
          <Badge
            variant="outline"
            className="h-5 px-1.5 text-[10px]"
            title="The model requested a tool that does not exist — refused because nothing by that name is registered"
          >
            unknown tool
          </Badge>
        ) : call.error ? (
          <Badge variant="outline" className="h-5 px-1.5 text-[10px]">
            failed
          </Badge>
        ) : call.cached ? (
          // Not a second consultation of the source. Labelled so the timeline
          // is not read as the agent having checked twice.
          <Badge
            variant="outline"
            className="h-5 px-1.5 text-[10px]"
            title="A repeat of an earlier identical call, served from the first result"
          >
            repeat
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

          {isUnknown ? (
            <p className="text-muted-foreground text-[11px] leading-relaxed">
              No tool by this name is registered, so the request was refused
              before any authorization check. The message is deliberately
              identical to a real denial: telling the model which tools exist
              elsewhere would let it probe another tenant&apos;s configuration.
            </p>
          ) : call.error ? (
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

function ToolIcon({
  name,
  denied,
  unknown,
}: {
  name: string;
  denied: boolean;
  unknown: boolean;
}) {
  const className = "size-3.5 shrink-0 text-muted-foreground";
  if (denied) return <ShieldOff className={className} />;
  if (unknown) return <HelpCircle className={className} />;
  if (name.includes("database")) return <Database className={className} />;
  if (name.includes("knowledge") || name.includes("search"))
    return <FileSearch className={className} />;
  if (name.includes("api")) return <Globe className={className} />;
  return <Wrench className={className} />;
}
