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
 *
 * The login page renders one of two forms depending on whether an identity
 * provider is configured (ADR 0009) — a paste-a-token textarea, or a redirect
 * button with no token field at all. This suite runs against whatever the
 * frontend is actually serving, by design (see playwright.config.ts), so it
 * detects which one is live rather than assuming the development form.
 *
 * `token` is only used on the paste-a-token path. It is still required so a
 * caller cannot forget it and get a silently-skipped assertion; pass any
 * non-empty string when Keycloak is configured and the OIDC flow — not
 * implemented here — is what needs testing instead.
 */
export async function signIn(page: Page, token: string): Promise<void> {
  await page.goto("/login");

  const tokenField = page.getByLabel(/token/i);
  const providerButton = page.getByRole("button", { name: /continue to sign in/i });

  // Whichever one shows up first is the one this deployment is actually
  // running. A fixed timeout ordering would make this test itself flaky.
  const outcome = await Promise.race([
    tokenField
      .waitFor({ state: "visible", timeout: 10_000 })
      .then(() => "paste" as const),
    providerButton
      .waitFor({ state: "visible", timeout: 10_000 })
      .then(() => "provider" as const),
  ]);

  if (outcome === "provider") {
    await signInViaProvider(page, providerButton);
    return;
  }

  await tokenField.fill(token);
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL(/\/(chat|agent)/, { timeout: 15_000 });
}

/**
 * Drive the Keycloak redirect flow: click through, fill its real login page,
 * and land back on the console. Credentials come from the environment only —
 * never a literal in this file — so a test file can never become a place a
 * password is checked into git.
 *
 * Set E2E_OIDC_USERNAME and E2E_OIDC_PASSWORD to exercise this path. Tests
 * that need it should skip cleanly when they are unset, the same way the
 * database-backed backend tests skip when Docker is not up — a missing
 * credential is an environment fact, not a failure.
 */
async function signInViaProvider(
  page: Page,
  providerButton: ReturnType<Page["getByRole"]>,
): Promise<void> {
  const username = process.env.E2E_OIDC_USERNAME;
  const password = process.env.E2E_OIDC_PASSWORD;
  if (!username || !password) {
    throw new Error(
      "This deployment has an identity provider configured. Set " +
        "E2E_OIDC_USERNAME and E2E_OIDC_PASSWORD to drive the Keycloak login, " +
        "or point E2E at a build with no identity provider configured.",
    );
  }

  await providerButton.click();
  // Keycloak's own page, a different origin — this is the point of the test.
  await page.waitForURL(/\/realms\/.+\/protocol\/openid-connect\/auth/, {
    timeout: 15_000,
  });
  // getByLabel on Keycloak's markup (label wraps a nested span, id/name/type
  // set after client-side setup) proved flaky on the exact-match password
  // locator even once the field was visibly present in a failure snapshot.
  // #username / #password are Keycloak's own stable field ids, unlikely to
  // change under a company theme, and do not depend on matching the label
  // text at exactly the moment the accessibility tree is queried.
  await page.locator("#username").waitFor({ state: "visible", timeout: 15_000 });
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(password);
  await page.locator("#kc-login").click();
  // Keycloak redirects to /callback, which redeems the code and forwards on.
  await page.waitForURL(/\/(chat|agent)/, { timeout: 15_000 });
}
