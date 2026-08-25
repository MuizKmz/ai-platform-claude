"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ArrowRight, CircleCheck, KeyRound, ShieldCheck, Terminal } from "lucide-react";

import { LiquidGlass } from "@/components/liquid-glass";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api, clearToken, setToken } from "@/lib/api";

/**
 * Sign in by pasting a token.
 *
 * There is no username-and-password form because there is no password: tokens
 * are minted by the CLI against a locally-issued signing key, and Phase 11
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

      <main className="mx-auto grid min-h-dvh w-full max-w-5xl items-center gap-10 px-6 py-10 lg:grid-cols-[minmax(0,1fr)_28rem] lg:gap-20">
        <section className="max-w-xl space-y-7">
          <div className="text-primary flex items-center gap-2 text-xs font-medium tracking-[0.16em] uppercase">
            <Terminal className="size-4" strokeWidth={2.25} />
            Enterprise AI Integration Platform
          </div>

          <div className="space-y-4">
            <h1 className="max-w-lg text-4xl font-semibold tracking-tight text-balance sm:text-5xl">
              Governed AI for the systems your team already uses.
            </h1>
            <p className="text-muted-foreground max-w-lg text-base leading-relaxed">
              Ask grounded questions across approved knowledge and business data,
              with permissions, citations, and human approval built in.
            </p>
          </div>

          <ul className="space-y-3 text-sm">
            <LoginBenefit>Tenant- and label-scoped data access</LoginBenefit>
            <LoginBenefit>Verified citations for document answers</LoginBenefit>
            <LoginBenefit>Human approval before connected-system writes</LoginBenefit>
          </ul>
        </section>

        <LiquidGlass profile="standard" className="w-full p-7 sm:p-8">
          <div className="mb-7 space-y-3">
            <div className="flex items-center gap-2.5">
              <span className="bg-primary/15 text-primary flex size-9 items-center justify-center rounded-md">
                <KeyRound className="size-4" strokeWidth={2.25} />
              </span>
              <div>
                <p className="text-xs font-medium tracking-wide uppercase">Development console</p>
                <h2 className="text-xl font-semibold tracking-tight">Sign in to EAIP</h2>
              </div>
            </div>
            <p className="text-muted-foreground text-sm leading-relaxed">
              Paste the access token issued for your EAIP user. It determines the
              data and tools available to you.
            </p>
          </div>

          <form onSubmit={submit} className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="token" className="text-sm font-medium">
                Access token
              </Label>
              <Textarea
                id="token"
                value={token}
                onChange={(e) => setTokenValue(e.target.value)}
                placeholder="eyJhbGciOiJIUzI1NiIs…"
                // Monospace because a JWT is read character by character when
                // something is wrong with it.
                className="bg-background/40 h-32 resize-none font-mono text-xs"
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
              <ArrowRight className="size-4" />
              {checking ? "Checking…" : "Sign in"}
            </Button>
          </form>

          <div className="text-muted-foreground/70 mt-6 flex gap-2 border-t pt-4 text-xs leading-relaxed">
            <ShieldCheck className="text-primary mt-0.5 size-4 shrink-0" />
            <p>
            Tokens expire after 60 minutes and are held in this tab only. Phase 11
            replaces this with a real identity provider.
            </p>
          </div>
        </LiquidGlass>
      </main>
    </>
  );
}

function LoginBenefit({ children }: { children: React.ReactNode }) {
  return (
    <li className="text-muted-foreground flex items-center gap-2.5">
      <CircleCheck className="text-primary size-4 shrink-0" />
      {children}
    </li>
  );
}
