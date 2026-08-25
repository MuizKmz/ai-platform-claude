"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2, ShieldAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { setToken, setRefreshToken } from "@/lib/api";
import { completeLogin, consumeReturnTo } from "@/lib/oidc";

/**
 * Where the identity provider sends the browser back to.
 *
 * This page exists only to redeem the authorization code and get out of the
 * way. It renders a spinner because it should be visible for well under a
 * second; if someone reads this text, something is slow or broken.
 */
export default function CallbackPage() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  // React 19 runs effects twice in development. Redeeming an authorization code
  // twice fails the second time - they are single-use by design - so the guard
  // is what keeps a working sign-in from showing an error in dev.
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;

    const params = new URLSearchParams(window.location.search);
    completeLogin(params)
      .then((tokens) => {
        setToken(tokens.access_token);
        if (tokens.refresh_token) setRefreshToken(tokens.refresh_token);
        // replace, not push: the URL holds a spent authorization code, and
        // leaving it in history invites a back-button retry that cannot work.
        router.replace(consumeReturnTo());
      })
      .catch((cause: unknown) => {
        setError(cause instanceof Error ? cause.message : "Sign-in failed.");
      });
  }, [router]);

  if (error) {
    return (
      <>
        <div className="gradient-mesh" aria-hidden />
        <main className="flex min-h-dvh items-center justify-center px-6">
          <div className="border-border/60 bg-card/60 w-full max-w-md space-y-4 rounded-lg border p-8 backdrop-blur-xl">
            <div className="flex items-center gap-2.5">
              <ShieldAlert className="text-destructive size-5" strokeWidth={2.25} />
              <h1 className="text-lg font-semibold tracking-tight">Sign-in failed</h1>
            </div>
            <p className="text-muted-foreground text-sm leading-relaxed">{error}</p>
            <Button className="w-full" onClick={() => router.replace("/login")}>
              Back to sign in
            </Button>
          </div>
        </main>
      </>
    );
  }

  return (
    <>
      <div className="gradient-mesh" aria-hidden />
      <main className="flex min-h-dvh items-center justify-center px-6">
        <div className="text-muted-foreground flex items-center gap-2.5 text-sm">
          <Loader2 className="size-4 animate-spin" />
          Completing sign-in…
        </div>
      </main>
    </>
  );
}
