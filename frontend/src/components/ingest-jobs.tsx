"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, CircleDashed, Loader2, XCircle } from "lucide-react";

import { api, type IngestJob } from "@/lib/api";

/**
 * Ingestion jobs, with live progress while one is running.
 *
 * Polling rather than websockets. A job that takes minutes needs an update
 * every few seconds, not every few milliseconds, and a websocket would add a
 * connection to manage, reconnect, and authenticate for no gain at that
 * cadence. Polling stops entirely when nothing is running, so the idle cost is
 * zero rather than an open socket.
 *
 * Progress matters more than it looks: without files_done over files_total, a
 * long ingestion is indistinguishable from a hung one, and the usual response
 * to that is to start it again.
 */

const ACTIVE = new Set(["queued", "running"]);
const POLL_MS = 2000;

export function IngestJobs({ refreshKey }: { refreshKey: number }) {
  const [jobs, setJobs] = useState<IngestJob[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll() {
      try {
        const current = await api.jobs();
        if (cancelled) return;
        setJobs(current);
        setError(null);

        // Only keep polling while something is actually in flight. An idle
        // console should make no requests at all.
        if (current.some((job) => ACTIVE.has(job.status))) {
          timer = setTimeout(() => void poll(), POLL_MS);
        }
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Could not load jobs.");
      }
    }

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [refreshKey]);

  if (error) {
    return <p className="text-destructive text-xs">{error}</p>;
  }

  if (jobs.length === 0) {
    return (
      <p className="text-muted-foreground text-sm">
        No ingestion jobs yet.
      </p>
    );
  }

  return (
    <ul className="space-y-2">
      {jobs.slice(0, 8).map((job) => (
        <JobRow key={job.id} job={job} />
      ))}
    </ul>
  );
}

function JobRow({ job }: { job: IngestJob }) {
  const active = ACTIVE.has(job.status);
  const progress =
    job.files_total > 0 ? (job.files_done / job.files_total) * 100 : 0;

  return (
    <li className="border-border/50 bg-card/30 rounded-md border px-3 py-2.5">
      <div className="flex items-center gap-2.5">
        <StatusIcon status={job.status} />

        <div className="min-w-0 flex-1">
          <p className="truncate font-mono text-xs">{job.source_path}</p>
          <p className="text-muted-foreground text-[11px]">
            {active
              ? `${job.files_done} of ${job.files_total} files`
              : `${job.documents_created} ingested · ${job.chunks_created} chunks`}
          </p>
        </div>

        <span className="text-muted-foreground font-mono text-[11px]">
          {job.status}
        </span>
      </div>

      {active && job.files_total > 0 ? (
        <div
          className="bg-secondary/60 mt-2 h-1 overflow-hidden rounded-full"
          role="progressbar"
          aria-valuenow={Math.round(progress)}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`Ingesting ${job.source_path}`}
        >
          <div
            className="bg-primary h-full transition-[width] duration-500"
            style={{ width: `${progress}%` }}
          />
        </div>
      ) : null}

      {job.error ? (
        // The backend deliberately returns a generic message here — parser and
        // provider errors carry file paths and request content. Showing it
        // verbatim keeps that property.
        <p className="text-destructive mt-1.5 text-[11px]">{job.error}</p>
      ) : null}
    </li>
  );
}

function StatusIcon({ status }: { status: IngestJob["status"] }) {
  const className = "size-4 shrink-0";
  switch (status) {
    case "running":
      return <Loader2 className={`${className} text-primary animate-spin`} />;
    case "succeeded":
      return <CheckCircle2 className={`${className} text-primary`} />;
    case "failed":
      return <XCircle className={`${className} text-destructive`} />;
    default:
      return <CircleDashed className={`${className} text-muted-foreground`} />;
  }
}
