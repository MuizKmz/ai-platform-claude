"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Database,
  Globe,
  Loader2,
  Pencil,
  Plug,
  Plus,
  Trash2,
  Zap,
} from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { IntegrationForm } from "@/components/integration-form";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import {
  ApiError,
  type Integration,
  type IntegrationTestResult,
  api,
} from "@/lib/api";

/**
 * The integrations control plane.
 *
 * Until this page existed, connectors were configured in code and changed by
 * redeploying. The roadmap is direct about why that is a deliverable rather
 * than a nicety: configuring connectors through .env files and curl "stops
 * scaling at about two connectors".
 *
 * Nothing here decides what a caller may do. The API is admin-only and returns
 * 403 to everyone else; this page shows whatever the API gives it and renders
 * the error when it gives nothing. Hiding a button based on a role would be a
 * courtesy that reads like a control.
 */
export default function IntegrationsPage() {
  const [items, setItems] = useState<Integration[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Integration | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, IntegrationTestResult>>({});

  const load = useCallback(async () => {
    try {
      setItems(await api.integrations());
      setError(null);
    } catch (e) {
      setItems([]);
      setError(
        e instanceof ApiError
          ? e.message
          : "Could not load integrations. Is the backend running?",
      );
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function test(item: Integration) {
    setTesting(item.id);
    try {
      const result = await api.testIntegration(item.id);
      setResults((r) => ({ ...r, [item.id]: result }));
      // The probe records its outcome on the row, so refresh to pick up
      // last_tested_at rather than inventing a local copy of it.
      await load();
    } catch (e) {
      setResults((r) => ({
        ...r,
        [item.id]: {
          ok: false,
          // A 429 is expected behaviour rather than a fault: the probe is
          // rate-limited because it is a network probe.
          error:
            e instanceof ApiError && e.status === 429
              ? "Rate limited — wait a minute before testing again."
              : e instanceof ApiError
                ? e.message
                : "The test could not be run.",
          duration_ms: 0,
        },
      }));
    } finally {
      setTesting(null);
    }
  }

  async function remove(item: Integration) {
    if (!confirm(`Delete "${item.display_name}"? This cannot be undone.`)) return;
    try {
      await api.deleteIntegration(item.id);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not delete the integration.");
    }
  }

  return (
    <AppShell>
      <div className="mx-auto max-w-4xl px-6 py-6">
        <header className="mb-5 flex items-start justify-between gap-4">
          <div>
            <h1 className="text-base font-semibold tracking-tight">Integrations</h1>
            <p className="text-muted-foreground text-xs">
              Read-only connectors. Credentials are encrypted and never displayed.
            </p>
          </div>
          <Button
            size="sm"
            onClick={() => {
              setEditing(null);
              setFormOpen(true);
            }}
          >
            <Plus className="size-3.5" />
            Add
          </Button>
        </header>

        {error ? (
          <div className="border-destructive/40 bg-destructive/5 mb-4 flex gap-2.5 rounded-lg border px-3.5 py-2.5">
            <AlertCircle className="text-destructive mt-0.5 size-4 shrink-0" />
            <p className="text-sm">{error}</p>
          </div>
        ) : null}

        {items === null ? (
          <div className="space-y-2">
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-20 w-full" />
          </div>
        ) : items.length === 0 && !error ? (
          <EmptyState />
        ) : (
          <ul className="space-y-2">
            {items.map((item) => (
              <IntegrationRow
                key={item.id}
                item={item}
                testing={testing === item.id}
                result={results[item.id]}
                onTest={() => void test(item)}
                onEdit={() => {
                  setEditing(item);
                  setFormOpen(true);
                }}
                onDelete={() => void remove(item)}
              />
            ))}
          </ul>
        )}
      </div>

      {formOpen ? (
        <IntegrationForm
          // Remounts per target, so the form's state starts from the right row
          // rather than from whatever was open last.
          key={editing?.id ?? "new"}
          open={formOpen}
          onOpenChange={setFormOpen}
          existing={editing}
          onSaved={() => void load()}
        />
      ) : null}
    </AppShell>
  );
}

function IntegrationRow({
  item,
  testing,
  result,
  onTest,
  onEdit,
  onDelete,
}: {
  item: Integration;
  testing: boolean;
  result?: IntegrationTestResult;
  onTest: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const Icon = item.kind === "sql" ? Database : Globe;
  // Prefer this session's result; fall back to what the last probe recorded, so
  // a page reload does not lose the health an operator just established.
  const ok = result ? result.ok : item.last_test_ok;
  const errorText = result ? result.error : item.last_test_error;

  return (
    <li
      className={cn(
        "border-border/60 bg-card/40 rounded-lg border p-3.5",
        !item.enabled && "opacity-60",
      )}
    >
      <div className="flex items-start gap-3">
        <Icon className="text-muted-foreground mt-0.5 size-4 shrink-0" />

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">{item.display_name}</span>
            <Badge variant="outline" className="h-4 px-1.5 font-mono text-[10px] font-normal">
              {item.slug}
            </Badge>
            {!item.enabled ? (
              <Badge variant="outline" className="h-4 px-1.5 text-[10px]">
                disabled
              </Badge>
            ) : null}
            {!item.has_credential ? (
              <Badge
                variant="outline"
                className="h-4 px-1.5 text-[10px]"
                title="No credential stored"
              >
                no credential
              </Badge>
            ) : null}
          </div>

          <p className="text-muted-foreground mt-1 font-mono text-[11px]">
            {item.kind === "sql"
              ? `${item.settings.username ?? "?"}@${item.settings.host ?? "?"}:${item.settings.port ?? "?"}/${item.settings.database ?? "?"}`
              : String(item.settings.base_url ?? "")}
          </p>

          <div className="text-muted-foreground/80 mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
            <span>
              {/* Empty labels means nobody, and that is worth saying out loud
                  rather than rendering as a blank space. */}
              {item.required_labels.length > 0
                ? `labels: ${item.required_labels.join(" · ")}`
                : "no labels — reachable by nobody"}
            </span>
            {item.last_tested_at ? (
              <span>tested {new Date(item.last_tested_at).toLocaleString()}</span>
            ) : (
              <span>never tested</span>
            )}
          </div>

          {ok !== null && ok !== undefined ? (
            <div
              className={cn(
                "mt-2 flex items-center gap-1.5 text-[11px]",
                ok ? "text-emerald-500" : "text-destructive",
              )}
            >
              {ok ? (
                <>
                  <CheckCircle2 className="size-3.5" />
                  Connected
                  {result ? ` in ${Math.round(result.duration_ms)}ms` : ""}
                </>
              ) : (
                <>
                  <AlertCircle className="size-3.5" />
                  {errorText ?? "Connection failed"}
                </>
              )}
            </div>
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={onTest}
            disabled={testing}
            title="Probe this connector. Rate-limited to 5 per minute."
          >
            {testing ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Zap className="size-3.5" />
            )}
            Test
          </Button>
          <Button variant="ghost" size="sm" onClick={onEdit} aria-label="Edit">
            <Pencil className="size-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onDelete}
            aria-label="Delete"
            className="text-muted-foreground hover:text-destructive"
          >
            <Trash2 className="size-3.5" />
          </Button>
        </div>
      </div>
    </li>
  );
}

function EmptyState() {
  return (
    <div className="border-border/50 rounded-lg border border-dashed px-6 py-10 text-center">
      <Plug className="text-muted-foreground/50 mx-auto size-6" />
      <p className="mt-3 text-sm font-medium">No integrations yet</p>
      <p className="text-muted-foreground mx-auto mt-1 max-w-sm text-xs leading-relaxed">
        A connector gives the agent a source beyond documents — a read-only
        database, or a GET-only API. Credentials are encrypted before storage
        and are never returned by the API, so keep a copy where you keep your
        other secrets.
      </p>
    </div>
  );
}
