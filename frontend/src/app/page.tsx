"use client";

import { useEffect, useState } from "react";
import { Activity, Database, FileText, Terminal } from "lucide-react";

import { LiquidGlass } from "@/components/liquid-glass";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { api } from "@/lib/api";

/**
 * Landing page: does the backend answer, and what is this thing.
 *
 * Also the reference for how the two surface types are used. Glass on chrome —
 * status panels, navigation, floating cards. Solid on data, which arrives with
 * the tables in stage 3: dense text read through a lens is the one place the
 * aesthetic fights the tool.
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
    <>
      {/* The backdrop the glass refracts. Without structure behind it, a glass
          panel is just a grey rectangle. */}
      <div className="gradient-mesh" aria-hidden />

      <main className="mx-auto w-full max-w-5xl px-6 py-16">
        <header className="mb-10">
          <div className="flex items-center gap-2.5">
            <Terminal className="text-primary size-5" strokeWidth={2.25} />
            <h1 className="text-xl font-semibold tracking-tight">EAIP Console</h1>
          </div>
          <p className="text-muted-foreground mt-2 text-sm">
            Control plane for the Enterprise AI Integration Platform.
          </p>
        </header>

        <LiquidGlass profile="standard" className="mb-8 p-6">
          <div className="mb-4 flex items-center gap-2">
            <Activity className="text-muted-foreground size-4" />
            <h2 className="text-sm font-medium">Backend status</h2>
          </div>

          {error ? (
            <div className="space-y-2">
              <Badge variant="destructive">unreachable</Badge>
              <p className="text-muted-foreground font-mono text-xs">{error}</p>
              <p className="text-muted-foreground text-xs">
                Start it with{" "}
                <code className="text-foreground font-mono">
                  uv run uvicorn app.main:app --reload
                </code>{" "}
                from{" "}
                <code className="text-foreground font-mono">backend/</code>.
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
        </LiquidGlass>

        <Separator className="my-8 opacity-40" />

        <section>
          <h2 className="text-muted-foreground mb-4 text-xs font-medium uppercase tracking-wider">
            Coming in this phase
          </h2>
          <div className="grid gap-4 sm:grid-cols-2">
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

        <p className="text-muted-foreground/70 mt-12 text-xs leading-relaxed">
          Panels use refraction where the browser supports SVG filters in{" "}
          <code className="font-mono">backdrop-filter</code> — currently Chromium
          only. Elsewhere they fall back to a blur, which changes how the
          material looks and nothing about how it works.
        </p>
      </main>
    </>
  );
}

function StatusPill({ label, value }: { label: string; value: string }) {
  const ok = value === "ok";
  return (
    <div className="border-border/60 bg-secondary/30 flex items-center gap-2 rounded-md border px-2.5 py-1.5">
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
    <LiquidGlass profile="subtle" className="p-5">
      <div className="text-muted-foreground mb-2 flex items-center gap-2 text-sm font-medium">
        {icon}
        {title}
      </div>
      <p className="text-muted-foreground text-sm leading-relaxed">
        {description}
      </p>
    </LiquidGlass>
  );
}
