"use client";

import { useEffect, useState } from "react";
import { Activity, Database, FileText, Terminal } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { api } from "@/lib/api";

/**
 * Landing page: does the backend answer, and what is this thing.
 *
 * Deliberately small. It exists to prove the frontend can reach FastAPI and to
 * establish the visual language before the real screens are built on top of it.
 */
export default function Home() {
  const [health, setHealth] = useState<{
    status: string;
    db: string;
    redis: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .health()
      .then(setHealth)
      .catch((e: Error) => setError(e.message));
  }, []);

  return (
    <main className="mx-auto w-full max-w-5xl px-6 py-16">
      <header className="mb-12">
        <div className="flex items-center gap-2.5">
          <Terminal className="text-primary size-5" strokeWidth={2.25} />
          <h1 className="text-xl font-semibold tracking-tight">EAIP Console</h1>
        </div>
        <p className="text-muted-foreground mt-2 text-sm">
          Control plane for the Enterprise AI Integration Platform.
        </p>
      </header>

      <Card className="mb-8">
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-sm font-medium">
            <Activity className="text-muted-foreground size-4" />
            Backend status
          </CardTitle>
        </CardHeader>
        <CardContent>
          {error ? (
            <div className="space-y-1.5">
              <Badge variant="destructive">unreachable</Badge>
              <p className="text-muted-foreground font-mono text-xs">{error}</p>
              <p className="text-muted-foreground text-xs">
                Start it with{" "}
                <code className="text-foreground font-mono">
                  uv run uvicorn app.main:app --reload
                </code>{" "}
                from <code className="text-foreground font-mono">backend/</code>.
              </p>
            </div>
          ) : health ? (
            <div className="flex flex-wrap items-center gap-2">
              <StatusPill label="api" value={health.status} />
              <StatusPill label="postgres" value={health.db} />
              <StatusPill label="redis" value={health.redis} />
            </div>
          ) : (
            <p className="text-muted-foreground text-sm">Checking…</p>
          )}
        </CardContent>
      </Card>

      <Separator className="my-8" />

      <section>
        <h2 className="text-muted-foreground mb-4 text-xs font-medium uppercase tracking-wider">
          Coming in this phase
        </h2>
        <div className="grid gap-3 sm:grid-cols-2">
          <PlannedCard
            icon={<FileText className="size-4" />}
            title="Knowledge"
            description="Upload, list, delete, and reindex documents. Watch ingestion jobs run."
          />
          <PlannedCard
            icon={<Database className="size-4" />}
            title="Integrations"
            description="Configure connectors, test connections, and see health at a glance."
          />
        </div>
      </section>
    </main>
  );
}

function StatusPill({ label, value }: { label: string; value: string }) {
  const ok = value === "ok";
  return (
    <div className="border-border bg-secondary/40 flex items-center gap-2 rounded-md border px-2.5 py-1.5">
      {/* A dot rather than a coloured pill: on a dense screen, colour should
          mark the exception, not every row. */}
      <span
        className={`size-1.5 rounded-full ${ok ? "bg-primary" : "bg-destructive"}`}
        aria-hidden
      />
      <span className="text-muted-foreground font-mono text-xs">{label}</span>
      <span className="font-mono text-xs">{value}</span>
    </div>
  );
}

function PlannedCard({
  icon,
  title,
  description,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
}) {
  return (
    <Card className="bg-card/60">
      <CardHeader className="pb-2">
        <CardTitle className="text-muted-foreground flex items-center gap-2 text-sm font-medium">
          {icon}
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-muted-foreground text-sm leading-relaxed">
          {description}
        </p>
      </CardContent>
    </Card>
  );
}
