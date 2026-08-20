"use client";

import { useEffect, useState } from "react";
import { FolderInput, Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { DocumentTable } from "@/components/document-table";
import { IngestJobs } from "@/components/ingest-jobs";
import { LiquidGlass } from "@/components/liquid-glass";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ApiError,
  type DocumentSummary,
  type Principal,
  api,
} from "@/lib/api";

/**
 * Documents and ingestion.
 *
 * Ingestion takes a server-side directory rather than a browser upload,
 * because that is what the API accepts today: the worker reads from a path the
 * server can see. A drag-and-drop upload needs an endpoint that receives bytes
 * and writes them somewhere the worker can reach, which is a backend change
 * rather than a frontend one, and inventing a fake upload here would be
 * misleading about what the platform does.
 */
export default function KnowledgePage() {
  const [documents, setDocuments] = useState<DocumentSummary[] | null>(null);
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [includeSuperseded, setIncludeSuperseded] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

  // The effect starts the fetch; the state updates land in the promise
  // continuation rather than synchronously inside the effect body. React 19
  // flags the latter as a cascading render, and it is right to — a setState
  // that runs during the effect re-triggers everything downstream of it.
  useEffect(() => {
    let cancelled = false;

    Promise.all([api.documents(includeSuperseded), api.me()])
      .then(([docs, me]) => {
        if (cancelled) return;
        setDocuments(docs);
        setPrincipal(me);
        setError(null);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Could not load documents.");
      });

    // Guards against a slow response landing after the user has navigated
    // away or changed the filter — the classic stale-write race.
    return () => {
      cancelled = true;
    };
  }, [includeSuperseded, refreshKey]);

  async function remove(id: string) {
    try {
      const result = await api.deleteDocument(id);
      toast.success(
        `Deleted. ${result.chunks_deleted} chunk${result.chunks_deleted === 1 ? "" : "s"} removed.`,
      );
      setRefreshKey((k) => k + 1);
    } catch (e) {
      // A caller without the admin role gets 403; one who cannot see the
      // document gets 404, which is deliberate — a 403 would confirm it
      // exists. Surfacing the API's own message preserves both.
      toast.error(
        e instanceof ApiError ? e.message : "Could not delete the document.",
      );
    }
  }

  // Deletion requires admin, and the API enforces it. Hiding the button for
  // everyone else is a courtesy that saves a pointless 403, never a control.
  const canDelete = principal?.roles.includes("admin") ?? false;

  return (
    <AppShell>
      <div className="mx-auto max-w-5xl px-6 py-6">
        <header className="mb-6 flex items-start justify-between gap-4">
          <div>
            <h1 className="text-base font-semibold tracking-tight">Knowledge</h1>
            <p className="text-muted-foreground text-xs">
              Documents your labels permit you to see.
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

        <div className="mb-3 flex items-center gap-2">
          <input
            id="superseded"
            type="checkbox"
            checked={includeSuperseded}
            onChange={(e) => setIncludeSuperseded(e.target.checked)}
            className="accent-primary size-3.5"
          />
          <Label
            htmlFor="superseded"
            className="text-muted-foreground text-xs font-normal"
          >
            Show superseded revisions
          </Label>
        </div>

        {documents === null ? (
          <div className="space-y-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : (
          <DocumentTable
            documents={documents}
            canDelete={canDelete}
            onDelete={remove}
          />
        )}

        <div className="mt-8 grid gap-4 lg:grid-cols-2">
          <IngestPanel
            canIngest={canDelete}
            onStarted={() => setRefreshKey((k) => k + 1)}
          />

          <LiquidGlass profile="subtle" className="p-5">
            <h2 className="mb-3 text-sm font-medium">Recent jobs</h2>
            <IngestJobs refreshKey={refreshKey} />
          </LiquidGlass>
        </div>
      </div>
    </AppShell>
  );
}

function IngestPanel({
  canIngest,
  onStarted,
}: {
  canIngest: boolean;
  onStarted: () => void;
}) {
  const [directory, setDirectory] = useState("");
  const [labels, setLabels] = useState("public");
  const [starting, setStarting] = useState(false);

  async function start(event: React.FormEvent) {
    event.preventDefault();
    const parsed = labels
      .split(",")
      .map((l) => l.trim())
      .filter(Boolean);

    setStarting(true);
    try {
      await api.startIngest(directory.trim(), parsed);
      toast.success("Ingestion queued.");
      setDirectory("");
      onStarted();
    } catch (e) {
      toast.error(
        e instanceof ApiError ? e.message : "Could not start ingestion.",
      );
    } finally {
      setStarting(false);
    }
  }

  return (
    <LiquidGlass profile="subtle" className="p-5">
      <h2 className="mb-3 flex items-center gap-2 text-sm font-medium">
        <FolderInput className="text-muted-foreground size-4" />
        Ingest a directory
      </h2>

      <form onSubmit={start} className="space-y-3">
        <div className="space-y-1.5">
          <Label htmlFor="directory" className="text-xs font-normal">
            Server path
          </Label>
          <Input
            id="directory"
            value={directory}
            onChange={(e) => setDirectory(e.target.value)}
            placeholder="C:\Users\you\Documents\corpus"
            className="bg-background/40 font-mono text-xs"
            disabled={!canIngest || starting}
          />
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="labels" className="text-xs font-normal">
            Labels
          </Label>
          <Input
            id="labels"
            value={labels}
            onChange={(e) => setLabels(e.target.value)}
            placeholder="public, finance"
            className="bg-background/40 font-mono text-xs"
            disabled={!canIngest || starting}
            aria-describedby="labels-help"
          />
          <p id="labels-help" className="text-muted-foreground/70 text-[11px]">
            Required. A document with no labels is visible to nobody.
          </p>
        </div>

        <Button
          type="submit"
          size="sm"
          disabled={!canIngest || starting || !directory.trim() || !labels.trim()}
        >
          {starting ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <FolderInput className="size-3.5" />
          )}
          Queue ingestion
        </Button>

        {!canIngest ? (
          <p className="text-muted-foreground/70 text-[11px]">
            Ingestion requires the admin role.
          </p>
        ) : null}
      </form>
    </LiquidGlass>
  );
}
