"use client";

import { useEffect, useState } from "react";
import { RefreshCw, ShieldAlert } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { TraceRow } from "@/components/trace-timeline";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ApiError, type AuditEntry, type Trace, api } from "@/lib/api";

/**
 * Traces and the connector audit log.
 *
 * Both are admin-only and the API says so — this page does not pre-check the
 * role. It renders the 403 it receives, which keeps the authorization decision
 * in one place rather than two that can disagree.
 */
export default function TracesPage() {
  const [traces, setTraces] = useState<Trace[] | null>(null);
  const [audit, setAudit] = useState<AuditEntry[] | null>(null);
  const [deniedOnly, setDeniedOnly] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    let cancelled = false;

    Promise.all([api.traces(25), api.audit(50, deniedOnly)])
      .then(([t, a]) => {
        if (cancelled) return;
        setTraces(t);
        setAudit(a);
        setError(null);
        setForbidden(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 403) {
          setForbidden(true);
          return;
        }
        setError(e instanceof Error ? e.message : "Could not load traces.");
      });

    return () => {
      cancelled = true;
    };
  }, [deniedOnly, refreshKey]);

  if (forbidden) {
    return (
      <AppShell>
        <div className="mx-auto max-w-3xl px-6 py-16">
          <div className="text-muted-foreground flex items-start gap-3">
            <ShieldAlert className="mt-0.5 size-5 shrink-0" />
            <div>
              <h1 className="text-foreground mb-1 text-sm font-medium">
                Admin only
              </h1>
              <p className="text-sm leading-relaxed">
                Traces name which requests were made and what they cost; audit
                rows carry the SQL people ran. Both are records about users
                rather than for them, so they need the admin role.
              </p>
            </div>
          </div>
        </div>
      </AppShell>
    );
  }

  return (
    <AppShell>
      <div className="mx-auto max-w-5xl px-6 py-6">
        <header className="mb-6 flex items-start justify-between gap-4">
          <div>
            <h1 className="text-base font-semibold tracking-tight">
              Traces &amp; audit
            </h1>
            <p className="text-muted-foreground text-xs">
              What the platform did, and what it refused to do.
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setRefreshKey((k) => k + 1)}
            className="text-muted-foreground"
          >
            <RefreshCw className="size-3.5" />
            Refresh
          </Button>
        </header>

        {error ? (
          <p role="alert" className="text-destructive mb-4 text-sm">
            {error}
          </p>
        ) : null}

        <Tabs defaultValue="traces">
          <TabsList className="mb-4">
            <TabsTrigger value="traces" className="text-xs">
              Traces
            </TabsTrigger>
            <TabsTrigger value="audit" className="text-xs">
              Connector audit
            </TabsTrigger>
          </TabsList>

          <TabsContent value="traces">
            {traces === null ? (
              <LoadingRows />
            ) : traces.length === 0 ? (
              <p className="text-muted-foreground py-8 text-sm">
                No traces yet. Ask something in Chat and it will appear here.
              </p>
            ) : (
              <ul className="space-y-2">
                {traces.map((trace) => (
                  <TraceRow key={trace.trace_id} trace={trace} />
                ))}
              </ul>
            )}
          </TabsContent>

          <TabsContent value="audit">
            <div className="mb-3 flex items-center gap-2">
              <input
                id="denied"
                type="checkbox"
                checked={deniedOnly}
                onChange={(e) => setDeniedOnly(e.target.checked)}
                className="accent-primary size-3.5"
              />
              <Label
                htmlFor="denied"
                className="text-muted-foreground text-xs font-normal"
              >
                Refusals only
              </Label>
            </div>

            {audit === null ? (
              <LoadingRows />
            ) : audit.length === 0 ? (
              <p className="text-muted-foreground py-8 text-sm">
                {deniedOnly
                  ? "No refused queries. That is the good outcome."
                  : "No connector queries yet."}
              </p>
            ) : (
              <ul className="space-y-2">
                {audit.map((entry) => (
                  <AuditRow key={entry.id} entry={entry} />
                ))}
              </ul>
            )}
          </TabsContent>
        </Tabs>
      </div>
    </AppShell>
  );
}

function AuditRow({ entry }: { entry: AuditEntry }) {
  return (
    <li
      className={
        // A refusal is marked, not alarming. Most of them are the safety layers
        // doing their job, and colouring every one red would make the log
        // unreadable exactly when it matters.
        entry.allowed
          ? "border-border/50 bg-card/30 rounded-md border px-3 py-2.5"
          : "border-destructive/30 bg-destructive/5 rounded-md border px-3 py-2.5"
      }
    >
      <div className="mb-1.5 flex items-center gap-2">
        <Badge
          variant={entry.allowed ? "secondary" : "destructive"}
          className="h-5 px-1.5 text-[10px] font-normal"
        >
          {entry.allowed ? "allowed" : "refused"}
        </Badge>
        <span className="text-muted-foreground text-xs">
          {entry.user_email}
        </span>
        <span className="text-muted-foreground/60 font-mono text-[11px]">
          {entry.connector_id}
        </span>
        <span className="text-muted-foreground/60 ml-auto font-mono text-[11px]">
          {new Date(entry.created_at).toLocaleTimeString()}
        </span>
      </div>

      {entry.question ? (
        <p className="text-muted-foreground mb-1.5 text-xs">
          “{entry.question}”
        </p>
      ) : null}

      {/* The SQL is the point of an audit row. Shown in full rather than
          truncated, because "what exactly did they run" is the question this
          table exists to answer. */}
      <pre className="text-foreground/80 overflow-x-auto font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
        {entry.sql || "(no SQL generated)"}
      </pre>

      {entry.denial_reason ? (
        <p className="text-destructive/90 mt-1.5 text-[11px]">
          {entry.denial_reason}
        </p>
      ) : null}

      {entry.allowed && entry.row_count !== null ? (
        <p className="text-muted-foreground/60 mt-1.5 font-mono text-[11px]">
          {entry.row_count} row{entry.row_count === 1 ? "" : "s"}
          {entry.duration_ms ? ` · ${Math.round(entry.duration_ms)}ms` : ""}
        </p>
      ) : null}
    </li>
  );
}

function LoadingRows() {
  return (
    <div className="space-y-2">
      <Skeleton className="h-11 w-full" />
      <Skeleton className="h-11 w-full" />
      <Skeleton className="h-11 w-full" />
    </div>
  );
}
