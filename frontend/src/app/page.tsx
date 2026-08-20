"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { getToken } from "@/lib/api";

/**
 * Root: send people where they belong.
 *
 * A client-side check rather than middleware, because the token lives in
 * sessionStorage and the server cannot see it. That also means this is a
 * routing convenience and not a guard — every page's data is protected by the
 * API, which is the only place that can enforce anything.
 */
export default function Root() {
  const router = useRouter();

  useEffect(() => {
    router.replace(getToken() ? "/chat" : "/login");
  }, [router]);

  return null;
}
