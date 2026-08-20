"use client";

import { useState } from "react";
import { Loader2 } from "lucide-react";

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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  ApiError,
  type Integration,
  type IntegrationCreate,
  type IntegrationUpdate,
  api,
} from "@/lib/api";

/**
 * Create or edit a connector.
 *
 * The credential field is the part worth explaining. When editing, it renders
 * empty with a note saying the stored value is kept — because the API will not
 * return it, and cannot: there is no endpoint that discloses a stored
 * credential. That is not a limitation to work around, it is the guarantee, so
 * the form is built to make "leave it alone" the effortless default and
 * replacing it a deliberate act.
 *
 * Settings are per-kind. Rather than one free-form JSON box, each kind gets
 * real fields — a JSON textarea would push validation errors to the API for
 * things a form can prevent, and invites pasting a credential into `settings`
 * where it would be stored unencrypted.
 */

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Absent when creating. */
  existing?: Integration | null;
  onSaved: () => void;
}

const KINDS = [
  { value: "sql", label: "SQL database (read-only)" },
  { value: "rest", label: "REST API (GET only)" },
] as const;

export function IntegrationForm({ open, onOpenChange, existing, onSaved }: Props) {
  const editing = Boolean(existing);

  const [kind, setKind] = useState(existing?.kind ?? "sql");
  const [slug, setSlug] = useState(existing?.slug ?? "");
  const [displayName, setDisplayName] = useState(existing?.display_name ?? "");
  const [labels, setLabels] = useState((existing?.required_labels ?? []).join(", "));
  const [credential, setCredential] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const s = (existing?.settings ?? {}) as Record<string, unknown>;
  const [host, setHost] = useState(String(s.host ?? ""));
  const [port, setPort] = useState(String(s.port ?? "5432"));
  const [database, setDatabase] = useState(String(s.database ?? ""));
  const [username, setUsername] = useState(String(s.username ?? ""));
  const [schemaName, setSchemaName] = useState(String(s.schema_name ?? "curated"));
  const [baseUrl, setBaseUrl] = useState(String(s.base_url ?? ""));
  const [allowPrivate, setAllowPrivate] = useState(Boolean(s.allow_private ?? false));
  const [allowLoopback, setAllowLoopback] = useState(Boolean(s.allow_loopback ?? false));

  function buildSettings(): Record<string, unknown> {
    if (kind === "sql") {
      return {
        host,
        // A blank field makes Number("") NaN, and JSON.stringify writes NaN
        // as a bare literal — invalid JSON, which the server rejects as a 400
        // with no useful message. Send null instead and let the API's
        // validation produce a readable 422.
        port: port.trim() === "" || Number.isNaN(Number(port)) ? null : Number(port),
        database,
        username,
        schema_name: schemaName,
        allow_private: allowPrivate,
        allow_loopback: allowLoopback,
      };
    }
    return {
      base_url: baseUrl,
      endpoints: (s.endpoints as unknown[]) ?? [],
      allow_private: allowPrivate,
      allow_loopback: allowLoopback,
    };
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const parsedLabels = labels
        .split(",")
        .map((l) => l.trim())
        .filter(Boolean);

      if (existing) {
        const body: IntegrationUpdate = {
          display_name: displayName,
          required_labels: parsedLabels,
          settings: buildSettings(),
        };
        // Only sent when the field was actually filled in. An empty string
        // would overwrite a working credential with nothing.
        if (credential) body.credential = credential;
        await api.updateIntegration(existing.id, body);
      } else {
        const body: IntegrationCreate = {
          kind,
          slug,
          display_name: displayName,
          required_labels: parsedLabels,
          settings: buildSettings(),
          credential: credential || null,
        };
        await api.createIntegration(body);
      }
      onSaved();
      onOpenChange(false);
    } catch (e) {
      // The API returns a readable message for configuration mistakes on
      // purpose — unlike authorization failures, a bad port SHOULD be legible
      // to whoever typed it.
      setError(e instanceof ApiError ? e.message : "Could not save the integration.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{editing ? "Edit integration" : "Add integration"}</DialogTitle>
          <DialogDescription>
            {editing
              ? "Changes take effect on the next request that uses this connector."
              : "Read-only access. Write operations are not available in this phase."}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          {!editing ? (
            <div className="grid grid-cols-2 gap-3">
              <Field label="Kind">
                <Select value={kind} onValueChange={setKind}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {KINDS.map((k) => (
                      <SelectItem key={k.value} value={k.value}>
                        {k.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
              <Field
                label="Slug"
                hint="Lowercase; becomes part of the tool name the model sees."
              >
                <Input
                  value={slug}
                  onChange={(e) => setSlug(e.target.value)}
                  placeholder="analytics"
                />
              </Field>
            </div>
          ) : null}

          <Field label="Display name">
            <Input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="Analytics warehouse"
            />
          </Field>

          <Field
            label="Required labels"
            hint="Comma-separated. A connector with no labels is reachable by nobody."
          >
            <Input
              value={labels}
              onChange={(e) => setLabels(e.target.value)}
              placeholder="analytics"
            />
          </Field>

          {kind === "sql" ? (
            <>
              <div className="grid grid-cols-3 gap-3">
                <div className="col-span-2">
                  <Field label="Host">
                    <Input value={host} onChange={(e) => setHost(e.target.value)} />
                  </Field>
                </div>
                <Field label="Port">
                  <Input
                    value={port}
                    onChange={(e) => setPort(e.target.value)}
                    inputMode="numeric"
                  />
                </Field>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Database">
                  <Input value={database} onChange={(e) => setDatabase(e.target.value)} />
                </Field>
                <Field label="Username" hint="Must be a read-only role.">
                  <Input value={username} onChange={(e) => setUsername(e.target.value)} />
                </Field>
              </div>
              <Field label="Schema" hint="Where the curated views live.">
                <Input value={schemaName} onChange={(e) => setSchemaName(e.target.value)} />
              </Field>
            </>
          ) : (
            <Field label="Base URL">
              <Input
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://api.example.com"
              />
            </Field>
          )}

          <Field
            label={editing ? "Replace credential" : "Credential"}
            hint={
              editing
                ? "Leave blank to keep the stored one. It cannot be displayed — the API has no endpoint that returns it."
                : "Encrypted before storage and never returned by any endpoint."
            }
          >
            <Input
              type="password"
              value={credential}
              onChange={(e) => setCredential(e.target.value)}
              placeholder={
                editing && existing?.has_credential ? "•••••••• (unchanged)" : ""
              }
              autoComplete="new-password"
            />
          </Field>

          <div className="border-border/60 space-y-2 rounded-md border p-3">
            <p className="text-muted-foreground text-[11px] leading-relaxed">
              Egress is blocked by default. Link-local addresses stay blocked
              regardless of these — cloud metadata is never reachable.
            </p>
            <Checkbox
              id="allow-private"
              checked={allowPrivate}
              onChange={setAllowPrivate}
              label="Allow private network addresses"
            />
            <Checkbox
              id="allow-loopback"
              checked={allowLoopback}
              onChange={setAllowLoopback}
              label="Allow loopback (local development only)"
            />
          </div>

          {error ? (
            <p role="alert" className="text-destructive text-xs">
              {error}
            </p>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={() => void save()} disabled={saving}>
            {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
            {editing ? "Save changes" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <Label className="text-xs">{label}</Label>
      {children}
      {hint ? <p className="text-muted-foreground/70 text-[11px]">{hint}</p> : null}
    </div>
  );
}

function Checkbox({
  id,
  checked,
  onChange,
  label,
}: {
  id: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="accent-primary size-3.5"
      />
      <Label htmlFor={id} className="text-[11px] font-normal">
        {label}
      </Label>
    </div>
  );
}
