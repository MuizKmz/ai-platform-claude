"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Clipboard,
  FileCode2,
  GraduationCap,
  Loader2,
  ShieldCheck,
  Sparkles,
} from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api, type Integration, type TrainingRecord } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Repository onboarding, not model fine-tuning.
 *
 * The prompt is deliberately copied into the target repository's own AI
 * workspace. EAIP never reads repository files or receives its secrets. The
 * returned JSON remains inactive until an administrator reviews and activates
 * the record.
 */
export default function TrainingPage() {
  const [records, setRecords] = useState<TrainingRecord[] | null>(null);
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [selected, setSelected] = useState<TrainingRecord | null>(null);
  const [profileText, setProfileText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [connectorId, setConnectorId] = useState("");
  const [repositoryRef, setRepositoryRef] = useState("");

  const load = useCallback(async () => {
    try {
      const [nextRecords, nextIntegrations] = await Promise.all([api.training(), api.integrations()]);
      setRecords(nextRecords);
      setIntegrations(nextIntegrations);
      setError(null);
    } catch (e) {
      setRecords([]);
      setError(e instanceof ApiError ? e.message : "Could not load Training. Is the backend running?");
    }
  }, []);

  useEffect(() => {
    // Deferring the initial request keeps this effect as a subscription to the
    // page lifecycle rather than a synchronous state cascade.
    const request = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(request);
  }, [load]);

  const healthyIntegrations = useMemo(
    () => integrations.filter((item) => item.enabled && item.has_credential && item.last_test_ok === true),
    [integrations],
  );

  const effectiveConnectorId = connectorId || healthyIntegrations[0]?.id || "";

  async function createIoTTraining() {
    if (!effectiveConnectorId) {
      setError("Choose a connected integration first.");
      return;
    }
    setBusy(true);
    try {
      const record = await api.createTraining({
        connector_id: effectiveConnectorId,
        system_type: "iot",
        environment: "test",
        repository_ref: repositoryRef.trim() || null,
        data_classification: "internal",
        description: "IoT system onboarding for approved read-only AI queries.",
      });
      const activeProfile = records?.find(
        (item) => item.connector_id === record.connector_id && item.status === "active",
      )?.submitted_profile;
      await load();
      setSelected(record);
      // A new revision normally improves an existing system map. Start from
      // the active profile so an admin can add one confirmed term rather than
      // having to reconstruct a long, already reviewed JSON document.
      setProfileText(activeProfile ? JSON.stringify(activeProfile, null, 2) : "");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not generate the training prompt.");
    } finally {
      setBusy(false);
    }
  }

  function open(record: TrainingRecord) {
    setSelected(record);
    setProfileText(record.submitted_profile ? JSON.stringify(record.submitted_profile, null, 2) : "");
  }

  async function copyPrompt() {
    if (!selected) return;
    try {
      await navigator.clipboard.writeText(selected.generated_prompt);
    } catch {
      setError("Could not copy automatically. Select the prompt and copy it manually.");
    }
  }

  async function submit() {
    if (!selected) return;
    let submitted: Record<string, unknown>;
    try {
      submitted = JSON.parse(profileText) as Record<string, unknown>;
    } catch {
      setError("The repository response must be valid JSON. Do not include a markdown code fence.");
      return;
    }
    const profile = mergeTrainingSnippet(selected, records ?? [], submitted);
    if (!profile) {
      setError(
        "A single term or metric needs an existing active IoT profile to merge into. " +
          "Paste the complete profile instead.",
      );
      return;
    }
    setBusy(true);
    try {
      const updated = await api.submitTraining(selected.id, profile);
      setSelected(updated);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not submit the training response.");
    } finally {
      setBusy(false);
    }
  }

  async function activate() {
    if (!selected) return;
    setBusy(true);
    try {
      const updated = await api.activateTraining(selected.id);
      setSelected(updated);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not activate the reviewed training record.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AppShell>
      <div className="mx-auto max-w-5xl px-6 py-6">
        <header className="mb-5 flex items-start justify-between gap-5">
          <div>
            <h1 className="text-base font-semibold tracking-tight">Training</h1>
            <p className="text-muted-foreground mt-1 max-w-2xl text-xs leading-relaxed">
              Build a reviewed system map from an AI working inside your IoT repository. This
              does not fine-tune a model, expose repository secrets, or grant new permissions.
            </p>
          </div>
          <Badge variant="outline" className="gap-1.5 whitespace-nowrap">
            <ShieldCheck className="size-3.5" />
            review before use
          </Badge>
        </header>

        {error ? (
          <div className="border-destructive/40 bg-destructive/5 mb-4 flex gap-2.5 rounded-lg border px-3.5 py-2.5">
            <AlertCircle className="text-destructive mt-0.5 size-4 shrink-0" />
            <p className="text-sm">{error}</p>
          </div>
        ) : null}

        <section className="border-border/60 bg-card/40 mb-5 rounded-lg border p-4">
          <div className="flex items-start gap-3">
            <GraduationCap className="text-primary mt-0.5 size-5" />
            <div className="min-w-0 flex-1">
              <h2 className="text-sm font-medium">Start IoT onboarding</h2>
              <p className="text-muted-foreground mt-1 text-xs leading-relaxed">
                Choose the tested IoT integration, copy the secure prompt into its repository AI,
                then paste the JSON response back here for review.
              </p>
              <div className="mt-3 grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
                <select
                  className="border-input bg-background h-8 rounded-lg border px-2.5 text-sm"
                  value={effectiveConnectorId}
                  onChange={(event) => setConnectorId(event.target.value)}
                  aria-label="IoT integration"
                >
                  {healthyIntegrations.length === 0 ? <option value="">No tested integration available</option> : null}
                  {healthyIntegrations.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.display_name} ({item.slug})
                    </option>
                  ))}
                </select>
                <Input
                  value={repositoryRef}
                  onChange={(event) => setRepositoryRef(event.target.value)}
                  placeholder="Repository reference (optional)"
                  aria-label="Repository reference"
                />
                <Button size="sm" onClick={() => void createIoTTraining()} disabled={busy || !effectiveConnectorId}>
                  {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}
                  Generate prompt
                </Button>
              </div>
            </div>
          </div>
        </section>

        {records === null ? (
          <div className="space-y-2">
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-20 w-full" />
          </div>
        ) : records.length === 0 ? (
          <EmptyState />
        ) : (
          <section className="overflow-hidden rounded-lg border border-border/60">
            <div className="text-muted-foreground grid grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_auto_auto] gap-3 border-b px-4 py-2 text-[11px] font-medium uppercase tracking-wide">
              <span>System</span><span>Integration</span><span>Status</span><span />
            </div>
            {records.map((record) => (
              <div key={record.id} className="grid grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_auto_auto] items-center gap-3 border-b border-border/50 px-4 py-3 last:border-0">
                <div className="min-w-0">
                  <p className="text-sm font-medium uppercase">{record.system_type}</p>
                  <p className="text-muted-foreground truncate text-[11px]">{record.environment} · {record.data_classification}</p>
                </div>
                <div className="min-w-0">
                  <p className="truncate text-sm">{record.integration_name}</p>
                  <p className="text-muted-foreground font-mono text-[11px]">{record.integration_kind}</p>
                </div>
                <Status status={record.status} />
                <Button variant="ghost" size="sm" onClick={() => open(record)}>
                  {record.status === "prompt_ready" ? "Train" : "Open"}
                </Button>
              </div>
            ))}
          </section>
        )}

        {selected ? (
          <section className="border-primary/25 bg-card/55 mt-5 rounded-lg border p-4">
            <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-sm font-medium">{selected.integration_name} onboarding</h2>
                <p className="text-muted-foreground mt-1 text-xs">Status: {selected.status.replaceAll("_", " ")}</p>
              </div>
              {selected.status === "review_required" ? (
                <Button size="sm" onClick={() => void activate()} disabled={busy}>
                  {busy ? <Loader2 className="size-3.5 animate-spin" /> : <CheckCircle2 className="size-3.5" />}
                  Approve & activate
                </Button>
              ) : null}
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <div>
                <div className="mb-1.5 flex items-center justify-between">
                  <label className="text-xs font-medium">1. Secure repository prompt</label>
                  <Button variant="ghost" size="sm" onClick={() => void copyPrompt()}>
                    <Clipboard className="size-3.5" /> Copy
                  </Button>
                </div>
                <Textarea readOnly value={selected.generated_prompt} className="h-96 font-mono text-[11px] leading-relaxed" />
              </div>
              <div>
                <label className="mb-1.5 flex items-center gap-1.5 text-xs font-medium">
                  <FileCode2 className="size-3.5" /> 2. Paste repository JSON response
                </label>
                <Textarea
                  value={profileText}
                  onChange={(event) => setProfileText(event.target.value)}
                  readOnly={selected.status === "active" || selected.status === "superseded"}
                  placeholder='Paste the full JSON response, or one term/metric JSON snippet to add to the active profile.'
                  className="h-80 font-mono text-[11px] leading-relaxed"
                />
                <p className="text-muted-foreground mt-2 text-[11px] leading-relaxed">
                  You may paste the full repository JSON, or a single <code>term</code> / <code>metric</code>
                  object to add to the active profile. Submissions with credentials or connection strings are
                  rejected. Nothing becomes available to the Agent until you explicitly approve it.
                </p>
                {selected.status === "prompt_ready" || selected.status === "review_required" ? (
                  <Button className="mt-3" size="sm" onClick={() => void submit()} disabled={busy || !profileText.trim()}>
                    {busy ? <Loader2 className="size-3.5 animate-spin" /> : <ShieldCheck className="size-3.5" />}
                    Submit for review
                  </Button>
                ) : null}
              </div>
            </div>
          </section>
        ) : null}
      </div>
    </AppShell>
  );
}

function mergeTrainingSnippet(
  selected: TrainingRecord,
  records: TrainingRecord[],
  submitted: Record<string, unknown>,
): Record<string, unknown> | null {
  const required = ["system_summary", "business_terms", "metrics", "safe_read_capabilities", "safe_example_questions"];
  if (required.every((key) => key in submitted)) return submitted;

  const base = selected.submitted_profile ?? records.find(
    (record) => record.connector_id === selected.connector_id && record.status === "active",
  )?.submitted_profile;
  if (!base) return null;

  const merged = JSON.parse(JSON.stringify(base)) as Record<string, unknown>;
  const terms = Array.isArray(merged.business_terms) ? merged.business_terms : [];
  const metrics = Array.isArray(merged.metrics) ? merged.metrics : [];
  const termPatch = "term" in submitted ? [submitted] : submitted.business_terms;
  const metricPatch = "name" in submitted && "unit" in submitted ? [submitted] : submitted.metrics;

  if (Array.isArray(termPatch)) merged.business_terms = [...terms, ...termPatch];
  if (Array.isArray(metricPatch)) merged.metrics = [...metrics, ...metricPatch];
  return termPatch || metricPatch ? merged : null;
}

function Status({ status }: { status: TrainingRecord["status"] }) {
  const active = status === "active";
  return (
    <Badge className={cn("whitespace-nowrap", active && "bg-emerald-600 hover:bg-emerald-600")} variant={active ? "default" : "outline"}>
      {status.replaceAll("_", " ")}
    </Badge>
  );
}

function EmptyState() {
  return (
    <div className="border-border/50 rounded-lg border border-dashed px-6 py-10 text-center">
      <GraduationCap className="text-muted-foreground/50 mx-auto size-6" />
      <p className="mt-3 text-sm font-medium">No system training yet</p>
      <p className="text-muted-foreground mx-auto mt-1 max-w-md text-xs leading-relaxed">
        Start with the tested IoT connector. EAIP will create a safe discovery prompt you can use
        inside the IoT repository without giving EAIP repository or database credentials.
      </p>
    </div>
  );
}
