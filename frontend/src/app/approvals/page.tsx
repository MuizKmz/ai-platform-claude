"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Clock,
  Loader2,
  Undo2,
  X,
} from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { ApiError, type Approval, type DryRun, api } from "@/lib/api";

/**
 * The approval queue: where a proposed write waits for a human.
 *
 * The design principle here is that **nobody approves from the list**. The
 * summary row is for triage — what, who, when — and the Review button opens the
 * exact payload that would be sent. Approving a one-line summary is approving a
 * sentence, and the sentence is not what reaches the target system.
 *
 * So the Approve button lives inside the review dialog, below the payload,
 * where it cannot be reached without the payload having been on screen.
 */
export default function ApprovalsPage() {
  const [items, setItems] = useState<Approval[] | null>(null);
  const [pendingOnly, setPendingOnly] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reviewing, setReviewing] = useState<Approval | null>(null);

  const load = useCallback(
    (cancelled?: { current: boolean }) =>
      api
        .approvals(pendingOnly)
        .then((list) => {
          if (cancelled?.current) return;
          setItems(list);
          setError(null);
        })
        .catch((e: unknown) => {
          if (cancelled?.current) return;
          setItems([]);
          setError(
            e instanceof ApiError
              ? e.message
              : "Could not load approvals. Is the backend running?",
          );
        }),
    [pendingOnly],
  );

  useEffect(() => {
    const cancelled = { current: false };
    void load(cancelled);
    return () => {
      cancelled.current = true;
    };
  }, [load]);

  const pendingCount = (items ?? []).filter((a) => a.status === "pending").length;

  return (
    <AppShell>
      <div className="mx-auto max-w-4xl px-6 py-6">
        <header className="mb-5 flex items-start justify-between gap-4">
          <div>
            <h1 className="text-base font-semibold tracking-tight">Approvals</h1>
            <p className="text-muted-foreground text-xs">
              Proposed actions. Nothing here has happened yet unless it says so.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <input
              id="pending-only"
              type="checkbox"
              checked={pendingOnly}
              onChange={(e) => setPendingOnly(e.target.checked)}
              className="accent-primary size-3.5"
            />
            <Label
              htmlFor="pending-only"
              className="text-muted-foreground text-xs font-normal"
            >
              Awaiting decision only
            </Label>
          </div>
        </header>

        {error ? (
          <div className="border-destructive/40 bg-destructive/5 mb-4 flex gap-2.5 rounded-lg border px-3.5 py-2.5">
            <AlertCircle className="text-destructive mt-0.5 size-4 shrink-0" />
            <p className="text-sm">{error}</p>
          </div>
        ) : null}

        {pendingCount > 0 ? (
          <div className="border-border/60 bg-card/40 mb-4 flex items-center gap-2.5 rounded-lg border px-3.5 py-2.5">
            <Clock className="text-muted-foreground size-4 shrink-0" />
            <p className="text-sm">
              {pendingCount} action{pendingCount === 1 ? "" : "s"} awaiting a
              decision.
            </p>
          </div>
        ) : null}

        {items === null ? (
          <div className="space-y-2">
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-20 w-full" />
          </div>
        ) : items.length === 0 && !error ? (
          <EmptyState pendingOnly={pendingOnly} />
        ) : (
          <ul className="space-y-2">
            {items.map((item) => (
              <ApprovalRow
                key={item.id}
                item={item}
                onReview={() => setReviewing(item)}
                onChanged={() => void load()}
              />
            ))}
          </ul>
        )}
      </div>

      {reviewing ? (
        <ReviewDialog
          approval={reviewing}
          onClose={() => setReviewing(null)}
          onDecided={() => void load()}
        />
      ) : null}
    </AppShell>
  );
}

const STATUS_STYLES: Record<Approval["status"], string> = {
  pending: "border-amber-500/40 text-amber-500",
  approved: "border-border/60 text-muted-foreground",
  executed: "border-emerald-500/40 text-emerald-500",
  rejected: "border-border/60 text-muted-foreground",
  failed: "border-destructive/50 text-destructive",
  expired: "border-border/60 text-muted-foreground",
};

function ApprovalRow({
  item,
  onReview,
  onChanged,
}: {
  item: Approval;
  onReview: () => void;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const expired = new Date(item.expires_at) < new Date();
  const actionable = item.status === "pending" && !expired;

  async function undo() {
    if (!confirm(`Undo "${item.summary}"? This will attempt to reverse it.`)) return;
    setBusy(true);
    setError(null);
    try {
      await api.compensate(item.id);
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not undo the action.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="border-border/60 bg-card/40 rounded-lg border p-3.5">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              variant="outline"
              className={cn("h-5 px-1.5 text-[10px]", STATUS_STYLES[item.status])}
            >
              {item.status}
            </Badge>
            {item.compensated_at ? (
              <Badge variant="outline" className="h-5 px-1.5 text-[10px]">
                undone
              </Badge>
            ) : null}
            {expired && item.status === "pending" ? (
              <Badge variant="outline" className="h-5 px-1.5 text-[10px]">
                expired
              </Badge>
            ) : null}
            <span className="text-sm font-medium">{item.summary}</span>
          </div>

          <p className="text-muted-foreground mt-1 font-mono text-[11px]">
            {item.target_method} {item.target_path} · {item.connector_slug}
          </p>

          <div className="text-muted-foreground/80 mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
            <span>proposed by {item.requested_by_email}</span>
            <span>{new Date(item.created_at).toLocaleString()}</span>
            {item.decided_by_email ? (
              <span>
                {item.status === "rejected" ? "rejected" : "approved"} by{" "}
                {item.decided_by_email}
              </span>
            ) : null}
          </div>

          {item.decision_note ? (
            <p className="text-muted-foreground mt-1.5 text-[11px] italic">
              “{item.decision_note}”
            </p>
          ) : null}

          {item.error ? (
            <p className="text-destructive/90 mt-1.5 text-[11px]">{item.error}</p>
          ) : null}

          {item.status === "executed" && !item.compensated_at ? (
            <div className="mt-2 flex items-center gap-1.5 text-[11px] text-emerald-500">
              <CheckCircle2 className="size-3.5" />
              Done — the target returned {item.response_status}
            </div>
          ) : null}

          {error ? (
            <p role="alert" className="text-destructive mt-1.5 text-[11px]">
              {error}
            </p>
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-1">
          {actionable ? (
            // The only route to approving. There is deliberately no approve
            // button on this row: approving a summary is approving a sentence,
            // and the sentence is not what gets sent.
            <Button size="sm" onClick={onReview}>
              Review
            </Button>
          ) : null}
          {item.status === "executed" && !item.compensated_at ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void undo()}
              disabled={busy}
              title="Attempt to reverse this action"
            >
              {busy ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <Undo2 className="size-3.5" />
              )}
              Undo
            </Button>
          ) : null}
        </div>
      </div>
    </li>
  );
}

/**
 * The review dialog: the payload, then the decision.
 *
 * The payload is fetched fresh rather than taken from the list, because the
 * list deliberately does not carry it — and because what an approver reads must
 * come from the same field the executor will send.
 */
function ReviewDialog({
  approval,
  onClose,
  onDecided,
}: {
  approval: Approval;
  onClose: () => void;
  onDecided: () => void;
}) {
  const [dryRun, setDryRun] = useState<DryRun | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .dryRun(approval.id)
      .then((result) => {
        if (!cancelled) setDryRun(result);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(
          e instanceof ApiError
            ? e.message
            : "Could not load what this action would do.",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [approval.id]);

  async function decide(decision: "approve" | "reject") {
    setBusy(decision);
    setError(null);
    try {
      if (decision === "approve") {
        await api.approve(approval.id, note || undefined);
      } else {
        await api.reject(approval.id, note || undefined);
      }
      onDecided();
      onClose();
    } catch (e) {
      // A failed write leaves a record; the error explains what the target
      // said, and the row will show `failed`.
      setError(
        e instanceof ApiError ? e.message : "The decision could not be recorded.",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Review this action</DialogTitle>
          <DialogDescription>
            Approving sends the request below immediately. Nothing has happened
            yet.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div>
            <p className="text-sm font-medium">{approval.summary}</p>
            <p className="text-muted-foreground mt-1 text-xs">
              Proposed by {approval.requested_by_email}
              {approval.reason ? ` — “${approval.reason}”` : ""}
            </p>
          </div>

          {dryRun === null && !error ? (
            <Skeleton className="h-40 w-full" />
          ) : dryRun ? (
            <>
              <div className="border-border/60 bg-muted/30 rounded-md border p-3">
                <p className="text-muted-foreground/70 mb-1.5 text-[10px] tracking-wider uppercase">
                  Where it goes
                </p>
                <p className="font-mono text-xs break-all">
                  <span className="text-foreground font-medium">
                    {dryRun.method}
                  </span>{" "}
                  {dryRun.target_base_url}
                  {dryRun.path}
                </p>
              </div>

              <div>
                <p className="text-muted-foreground/70 mb-1.5 text-[10px] tracking-wider uppercase">
                  Exactly what is sent
                </p>
                {/* The literal payload, not a summary of it. This is the thing
                    being approved. */}
                <pre className="border-border/60 bg-muted/30 max-h-72 overflow-auto rounded-md border p-3 font-mono text-[11px] leading-relaxed">
                  {JSON.stringify(dryRun.payload, null, 2)}
                </pre>
              </div>

              {dryRun.target_reachable === false ? (
                // Shown BEFORE the approve button, because an action that
                // cannot succeed should not be authorised. The check resolves
                // the address; it contacts nothing.
                <div className="border-destructive/40 bg-destructive/5 flex gap-2.5 rounded-md border px-3 py-2.5">
                  <AlertCircle className="text-destructive mt-0.5 size-4 shrink-0" />
                  <div>
                    <p className="text-destructive text-xs font-medium">
                      This action cannot reach its target
                    </p>
                    <p className="text-muted-foreground mt-0.5 text-[11px]">
                      {dryRun.target_problem} Approving it would fail. Fix the
                      connector first.
                    </p>
                  </div>
                </div>
              ) : null}

              <p className="text-muted-foreground/70 font-mono text-[10px]">
                idempotency key {dryRun.idempotency_key.slice(0, 24)}… — sending
                twice creates one action, not two
              </p>
            </>
          ) : null}

          <div className="space-y-1.5">
            <Label htmlFor="note" className="text-xs">
              Note
            </Label>
            <Textarea
              id="note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Why you are approving or rejecting. Recorded against this decision."
              className="min-h-[64px] text-sm"
            />
          </div>

          {error ? (
            <p role="alert" className="text-destructive text-xs">
              {error}
            </p>
          ) : null}
        </div>

        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={onClose} disabled={busy !== null}>
            Cancel
          </Button>
          <Button
            variant="outline"
            onClick={() => void decide("reject")}
            disabled={busy !== null || !dryRun}
          >
            {busy === "reject" ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <X className="size-3.5" />
            )}
            Reject
          </Button>
          <Button
            onClick={() => void decide("approve")}
            disabled={
              busy !== null ||
              !dryRun?.actionable ||
              dryRun?.target_reachable === false
            }
          >
            {busy === "approve" ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Check className="size-3.5" />
            )}
            Approve and send
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function EmptyState({ pendingOnly }: { pendingOnly: boolean }) {
  return (
    <div className="border-border/50 rounded-lg border border-dashed px-6 py-10 text-center">
      <CheckCircle2 className="text-muted-foreground/50 mx-auto size-6" />
      <p className="mt-3 text-sm font-medium">
        {pendingOnly ? "Nothing awaiting a decision" : "No proposed actions yet"}
      </p>
      <p className="text-muted-foreground mx-auto mt-1 max-w-sm text-xs leading-relaxed">
        The agent can propose actions — raising a ticket, for instance — but it
        cannot perform one. Anything it proposes appears here first, and a
        person decides.
      </p>
    </div>
  );
}
