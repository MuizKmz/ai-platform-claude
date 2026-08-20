import { execFileSync } from "node:child_process";
import path from "node:path";
import type { Page } from "@playwright/test";

/**
 * Shared setup for the end-to-end tests.
 *
 * Tokens come from the same CLI a person would use, rather than being minted in
 * TypeScript from the signing key. Reimplementing token issuance here would mean
 * the tests could pass against a broken CLI, and the CLI is how anyone actually
 * gets into this console.
 */

const BACKEND = path.resolve(__dirname, "..", "..", "backend");

export const API_BASE = process.env.E2E_API_URL ?? "http://127.0.0.1:8000";

/** Mint a token for an existing user, exactly as the README instructs. */
export function issueToken(tenantSlug: string, email: string): string {
  const output = execFileSync(
    "uv",
    ["run", "python", "-m", "app.cli", "token", tenantSlug, email],
    { cwd: BACKEND, encoding: "utf-8" },
  );
  const token = output.trim().split(/\s+/).pop();
  if (!token || token.split(".").length !== 3) {
    throw new Error(`the CLI did not return a JWT: ${output.slice(0, 200)}`);
  }
  return token;
}

/**
 * Sign in through the real login form.
 *
 * Deliberately not by writing sessionStorage directly. The login page validates
 * the token against /v1/me before routing, so driving the form covers that
 * round trip — and a broken login is exactly the kind of thing a test that
 * skips login would not notice.
 */
export async function signIn(page: Page, token: string): Promise<void> {
  await page.goto("/login");
  await page.getByLabel(/token/i).fill(token);
  await page.getByRole("button", { name: /sign in|continue/i }).click();
  await page.waitForURL(/\/chat/, { timeout: 15_000 });
}
