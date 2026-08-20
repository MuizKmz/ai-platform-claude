"use client";

import { useCallback, useEffect, useState } from "react";
import { AlertCircle, Loader2, Plus, ShieldCheck, Trash2, UserIcon } from "lucide-react";

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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, type Principal, type User, api } from "@/lib/api";

/**
 * Users, roles, and labels.
 *
 * Labels are offered as checkboxes drawn from what documents and connectors
 * actually use, rather than as a text field. The API rejects a label nothing
 * uses — a typo authorizes nothing while looking exactly like success — and a
 * list of real options prevents the mistake instead of reporting it.
 *
 * The lockout guards live in the API, not here. This page disables the controls
 * it can predict will fail, because a disabled button explains itself better
 * than a 409; but the API refuses regardless, which is what makes it a control.
 */
export default function UsersPage() {
  const [users, setUsers] = useState<User[] | null>(null);
  const [labels, setLabels] = useState<string[]>([]);
  const [me, setMe] = useState<Principal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<User | null>(null);
  const [creating, setCreating] = useState(false);

  // `cancelled` guards against setting state after unmount, and settling in a
  // promise callback rather than synchronously in the effect body is what keeps
  // this from cascading renders — the same shape the traces page uses.
  const load = useCallback((cancelled?: { current: boolean }) => {
    return Promise.all([api.users(), api.knownLabels(), api.me()])
      .then(([list, known, principal]) => {
        if (cancelled?.current) return;
        setUsers(list);
        setLabels(known);
        setMe(principal);
        setError(null);
      })
      .catch((e: unknown) => {
        if (cancelled?.current) return;
        setUsers([]);
        setError(
          e instanceof ApiError
            ? e.message
            : "Could not load users. Is the backend running?",
        );
      });
  }, []);

  useEffect(() => {
    const cancelled = { current: false };
    void load(cancelled);
    return () => {
      cancelled.current = true;
    };
  }, [load]);

  const adminCount = (users ?? []).filter((u) => u.roles.includes("admin")).length;

  async function remove(user: User) {
    if (!confirm(`Delete ${user.email}? This cannot be undone.`)) return;
    try {
      await api.deleteUser(user.id);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not delete the user.");
    }
  }

  return (
    <AppShell>
      <div className="mx-auto max-w-4xl px-6 py-6">
        <header className="mb-5 flex items-start justify-between gap-4">
          <div>
            <h1 className="text-base font-semibold tracking-tight">Users</h1>
            <p className="text-muted-foreground text-xs">
              Labels decide what a person can retrieve. Empty means nothing.
            </p>
          </div>
          <Button size="sm" onClick={() => setCreating(true)}>
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

        {users === null ? (
          <div className="space-y-2">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
          </div>
        ) : (
          <ul className="space-y-2">
            {users.map((user) => {
              const isSelf = user.id === me?.user_id;
              const isLastAdmin = user.roles.includes("admin") && adminCount <= 1;
              return (
                <li
                  key={user.id}
                  className="border-border/60 bg-card/40 flex items-start gap-3 rounded-lg border p-3.5"
                >
                  {user.roles.includes("admin") ? (
                    <ShieldCheck className="text-muted-foreground mt-0.5 size-4 shrink-0" />
                  ) : (
                    <UserIcon className="text-muted-foreground mt-0.5 size-4 shrink-0" />
                  )}

                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium">{user.email}</span>
                      {user.roles.map((role) => (
                        <Badge
                          key={role}
                          variant={role === "admin" ? "default" : "outline"}
                          className="h-4 px-1.5 text-[10px]"
                        >
                          {role}
                        </Badge>
                      ))}
                      {isSelf ? (
                        <span className="text-muted-foreground/70 text-[11px]">you</span>
                      ) : null}
                    </div>
                    <p className="text-muted-foreground mt-1 font-mono text-[11px]">
                      {user.allowed_labels.length > 0
                        ? user.allowed_labels.join(" · ")
                        : "no labels — can retrieve nothing"}
                    </p>
                  </div>

                  <div className="flex shrink-0 items-center gap-1">
                    <Button variant="ghost" size="sm" onClick={() => setEditing(user)}>
                      Edit
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => void remove(user)}
                      aria-label="Delete"
                      // Predicted refusals, disabled with a reason. The API
                      // refuses these too — that is what makes it a control.
                      disabled={isSelf || isLastAdmin}
                      title={
                        isSelf
                          ? "You cannot delete your own account"
                          : isLastAdmin
                            ? "This is the last admin"
                            : "Delete"
                      }
                      className="text-muted-foreground hover:text-destructive"
                    >
                      <Trash2 className="size-3.5" />
                    </Button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {creating || editing ? (
        <UserDialog
          key={editing?.id ?? "new"}
          existing={editing}
          knownLabels={labels}
          isSelf={editing?.id === me?.user_id}
          isLastAdmin={Boolean(editing?.roles.includes("admin")) && adminCount <= 1}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={() => void load()}
        />
      ) : null}
    </AppShell>
  );
}

function UserDialog({
  existing,
  knownLabels,
  isSelf,
  isLastAdmin,
  onClose,
  onSaved,
}: {
  existing: User | null;
  knownLabels: string[];
  isSelf: boolean;
  isLastAdmin: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [email, setEmail] = useState(existing?.email ?? "");
  const [isAdmin, setIsAdmin] = useState(existing?.roles.includes("admin") ?? false);
  const [selected, setSelected] = useState<string[]>(existing?.allowed_labels ?? []);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Demoting this account would be refused by the API. Saying so up front is
  // kinder than a 409 after they have filled the form in.
  const adminLocked = Boolean(existing) && (isSelf || isLastAdmin) && isAdmin;

  // Labels this person holds that nothing currently uses. They arise honestly:
  // a document carrying the label was deleted, or the grant was a typo in the
  // first place. Either way the grant authorizes nothing today.
  //
  // Shown rather than dropped. Offering only known labels would silently
  // revoke these on the next save, and a permission disappearing because
  // someone opened a dialog is the kind of change nobody can later explain.
  const orphaned = (existing?.allowed_labels ?? []).filter(
    (l) => !knownLabels.includes(l),
  );
  const offered = [...knownLabels, ...orphaned];

  function toggle(label: string) {
    setSelected((s) => (s.includes(label) ? s.filter((l) => l !== label) : [...s, label]));
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const roles = isAdmin ? ["admin", "reader"] : ["reader"];
      if (existing) {
        await api.updateUser(existing.id, { roles, allowed_labels: selected });
      } else {
        await api.createUser({ email, roles, allowed_labels: selected });
      }
      onSaved();
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not save the user.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{existing ? "Edit user" : "Add user"}</DialogTitle>
          <DialogDescription>
            Labels take effect on the next token this person is issued.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          {!existing ? (
            <div className="space-y-1.5">
              <Label className="text-xs">Email</Label>
              <Input
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="person@example.com"
                type="email"
              />
            </div>
          ) : null}

          <div className="space-y-1.5">
            <Label className="text-xs">Role</Label>
            <div className="flex items-center gap-2">
              <input
                id="admin"
                type="checkbox"
                checked={isAdmin}
                onChange={(e) => setIsAdmin(e.target.checked)}
                disabled={adminLocked}
                className="accent-primary size-3.5"
              />
              <Label htmlFor="admin" className="text-[11px] font-normal">
                Administrator — may manage users and integrations
              </Label>
            </div>
            {adminLocked ? (
              <p className="text-muted-foreground/70 text-[11px]">
                {isSelf
                  ? "You cannot remove your own admin role — ask another admin."
                  : "This is the last admin. Promote someone else first."}
              </p>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <Label className="text-xs">Labels</Label>
            {knownLabels.length === 0 ? (
              <p className="text-muted-foreground text-[11px]">
                No labels exist yet. Ingest a document or add a connector first.
              </p>
            ) : (
              <div className="flex flex-wrap gap-2">
                {offered.map((label) => (
                  <button
                    key={label}
                    type="button"
                    onClick={() => toggle(label)}
                    title={
                      orphaned.includes(label)
                        ? "No document or connector uses this label — it grants nothing today"
                        : undefined
                    }
                    className={
                      selected.includes(label)
                        ? "bg-primary text-primary-foreground rounded-md px-2 py-1 font-mono text-[11px]"
                        : "border-border/60 text-muted-foreground hover:text-foreground rounded-md border px-2 py-1 font-mono text-[11px]"
                    }
                  >
                    {label}
                    {orphaned.includes(label) ? " ⚠" : ""}
                  </button>
                ))}
              </div>
            )}
            <p className="text-muted-foreground/70 text-[11px]">
              {/* Worth saying plainly: an empty set is not "everything". */}
              {selected.length === 0
                ? "No labels selected — this person will retrieve nothing."
                : `Can retrieve documents labelled ${selected.join(", ")}.`}
            </p>
            {orphaned.some((l) => selected.includes(l)) ? (
              <p className="text-muted-foreground/70 text-[11px]">
                ⚠ marks a label nothing currently uses. It is kept so saving does
                not silently revoke it, but it grants nothing today.
              </p>
            ) : null}
          </div>

          {error ? (
            <p role="alert" className="text-destructive text-xs">
              {error}
            </p>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={() => void save()} disabled={saving || (!existing && !email)}>
            {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
            {existing ? "Save changes" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
