"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { KeyRound, Terminal } from "lucide-react";

import { LiquidGlass } from "@/components/liquid-glass";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api, clearToken, setToken } from "@/lib/api";

/**
 * Sign in by pasting a token.
 *
 * There is no username-and-password form because there is no password: tokens
 * are minted by the CLI against a locally-issued signing key, and Phase 8
 * replaces that with a real identity provider. A login form implying a
 * credential check that does not happen would be worse than this — it would be
 * theatre.
 *
 * The token is validated by calling /v1/me rather than by inspecting it here.
 * Client-side JWT parsing would tell us what the token *claims*; only the
 * server can say whether the signature holds, and it is the only opinion that
 * matters.
 */
export default function LoginPage() {
  const router = useRouter();
  const [token, setTokenValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = token.trim();
    if (!trimmed) return;

    setChecking(true);
    setError(null);
    setToken(trimmed);

    try {
      await api.me();
      router.push("/chat");
    } catch (e) {
      clearToken();
      // The backend returns one fixed message for every auth failure so it
      // cannot be used as an oracle. Surfacing it verbatim preserves that;
      // guessing at a friendlier reason would undo it.
      setError(
        e instanceof ApiError && e.isAuthFailure
          ? "That token was not accepted."
          : e instanceof Error
            ? e.message
            : "Could not reach the API.",
      );
    } finally {
      setChecking(false);
    }
  }

  return (
    <>
      <div className="gradient-mesh" aria-hidden />

      <main className="flex min-h-dvh items-center justify-center px-6">
        <LiquidGlass profile="standard" className="w-full max-w-md p-8">
          <div className="mb-6 flex items-center gap-2.5">
            <Terminal className="text-primary size-5" strokeWidth={2.25} />
            <h1 className="text-lg font-semibold tracking-tight">EAIP Console</h1>
          </div>

          <form onSubmit={submit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="token" className="text-xs font-medium">
                Access token
              </Label>
              <Textarea
                id="token"
                value={token}
                onChange={(e) => setTokenValue(e.target.value)}
                placeholder="eyJhbGciOiJIUzI1NiIs…"
                // Monospace because a JWT is read character by character when
                // something is wrong with it.
                className="bg-background/40 h-28 resize-none font-mono text-xs"
                autoComplete="off"
                spellCheck={false}
                aria-describedby={error ? "token-error" : "token-help"}
                aria-invalid={error ? true : undefined}
              />
            </div>

            {error ? (
              <p
                id="token-error"
                role="alert"
                className="text-destructive text-xs"
              >
                {error}
              </p>
            ) : (
              <p id="token-help" className="text-muted-foreground text-xs leading-relaxed">
                Mint one from{" "}
                <code className="text-foreground font-mono">backend/</code>:
                <br />
                <code className="text-foreground font-mono">
                  uv run python -m app.cli token acme alice@acme.test
                </code>
              </p>
            )}

            <Button type="submit" className="w-full" disabled={checking || !token.trim()}>
              <KeyRound className="size-4" />
              {checking ? "Checking…" : "Sign in"}
            </Button>
          </form>

          <p className="text-muted-foreground/70 mt-6 text-xs leading-relaxed">
            Tokens expire after 60 minutes and are held in this tab only. Phase 8
            replaces this with a real identity provider.
          </p>
        </LiquidGlass>
      </main>
    </>
  );
}
