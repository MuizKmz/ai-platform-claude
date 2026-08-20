"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  Activity,
  CheckSquare,
  Database,
  FileText,
  LogOut,
  MessagesSquare,
  Sparkles,
  Terminal,
  Users,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { ApiError, type Principal, api, clearToken, getToken } from "@/lib/api";

/**
 * The frame around every authenticated page.
 *
 * Also the only place that redirects on an expired token. Scattering that
 * across pages produces races where two of them redirect at once, and a login
 * screen that flashes because a third is still resolving.
 *
 * Note what it does NOT do: hide navigation based on roles. The sidebar shows
 * what exists; the API decides what a caller may have. Hiding a link is a
 * courtesy, never a control, and treating it as one leads to a UI that
 * "protects" an endpoint nobody protected.
 */

const NAV = [
  { href: "/chat", label: "Chat", icon: MessagesSquare },
  { href: "/agent", label: "Agent", icon: Sparkles },
  { href: "/knowledge", label: "Knowledge", icon: FileText },
  { href: "/integrations", label: "Integrations", icon: Database },
  { href: "/approvals", label: "Approvals", icon: CheckSquare },
  { href: "/users", label: "Users", icon: Users },
  { href: "/traces", label: "Traces", icon: Activity },
] as const;

export function AppShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [resolved, setResolved] = useState(false);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    api
      .me()
      .then(setPrincipal)
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.isAuthFailure) {
          clearToken();
          router.replace("/login");
          return;
        }
        // A backend that is down is not a reason to sign out — the token may
        // be perfectly good. The page below renders its own error state.
        setPrincipal(null);
      })
      .finally(() => setResolved(true));
  }, [router]);

  function signOut() {
    clearToken();
    router.replace("/login");
  }

  return (
    <>
      <div className="gradient-mesh" aria-hidden />

      <div className="flex h-dvh overflow-hidden">
        {/* Darker than the content it frames, so the eye settles on the working
            area rather than the navigation. */}
        <aside className="bg-sidebar/70 border-sidebar-border flex h-full w-56 shrink-0 flex-col border-r backdrop-blur-xl">
          <div className="flex items-center gap-2.5 px-5 py-5">
            <Terminal className="text-primary size-4.5" strokeWidth={2.25} />
            <span className="text-sm font-semibold tracking-tight">EAIP</span>
          </div>

          <nav className="flex-1 px-2.5" aria-label="Main">
            {NAV.map(({ href, label, icon: Icon }) => {
              const active = pathname === href || pathname.startsWith(`${href}/`);
              return (
                <Link
                  key={href}
                  href={href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "mb-0.5 flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors",
                    active
                      ? "bg-sidebar-accent text-sidebar-accent-foreground"
                      : "text-muted-foreground hover:text-foreground hover:bg-sidebar-accent/50",
                  )}
                >
                  <Icon className="size-4" />
                  {label}
                </Link>
              );
            })}
          </nav>

          <div className="border-sidebar-border border-t px-3 py-3">
            {!resolved ? (
              <Skeleton className="h-9 w-full" />
            ) : principal ? (
              <div className="space-y-2">
                <div className="px-1.5">
                  <p className="truncate text-xs font-medium">{principal.email}</p>
                  {/* Labels are shown because they explain what a user can and
                      cannot see — the commonest confusion in a system where
                      two people get different answers to the same question. */}
                  <p className="text-muted-foreground truncate font-mono text-[11px]">
                    {principal.allowed_labels.join(" · ") || "no labels"}
                  </p>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={signOut}
                  className="text-muted-foreground hover:text-foreground w-full justify-start px-1.5"
                >
                  <LogOut className="size-3.5" />
                  Sign out
                </Button>
              </div>
            ) : (
              <p className="text-muted-foreground px-1.5 text-xs">
                Not connected
              </p>
            )}
          </div>
        </aside>

        <main className="min-w-0 flex-1 overflow-y-auto">{children}</main>
      </div>
    </>
  );
}
