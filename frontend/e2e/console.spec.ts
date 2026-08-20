import { expect, test } from "@playwright/test";

import { issueToken, signIn } from "./helpers";

/**
 * The console, driven as a person drives it.
 *
 * These tests exist because two bugs in Phase 6 were invisible to every other
 * kind of test we have:
 *
 *   - PATCH was missing from the CORS allowlist. Every curl returned 200,
 *     because curl sends no preflight. The browser's OPTIONS got a 400 and the
 *     real request was never sent — broken only in a browser, found only by a
 *     person clicking Save.
 *   - An empty port field would serialise as a bare `NaN` literal, which is not
 *     valid JSON, producing a 400 with no useful message.
 *
 * So the rule for this file: never stub the network. A mocked E2E test is a
 * slower unit test that cannot see either of those.
 */

const ADMIN = { tenant: "acme", email: "admin@acme.test" };
const READER = { tenant: "acme", email: "carol@acme.test" };

test.describe("authentication", () => {
  test("an unauthenticated visitor is sent to login", async ({ page }) => {
    await page.goto("/chat");
    await expect(page).toHaveURL(/\/login/);
  });

  test("a bad token is refused without saying why", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel(/access token/i).fill("not.a.jwt");
    await page.getByRole("button", { name: /sign in/i }).click();

    // .first(): a toast and the inline message both carry role="alert", and
    // either one satisfies what this asserts.
    const alert = page.getByRole("alert").first();
    await expect(alert).toBeVisible();
    // The backend returns one fixed message for every auth failure so it cannot
    // be used as an oracle. The UI must not improve on it.
    await expect(page.getByText(/not accepted/i).first()).toBeVisible();
    await expect(page).toHaveURL(/\/login/);
  });

  test("a valid token signs in and lands on chat", async ({ page }) => {
    await signIn(page, issueToken(ADMIN.tenant, ADMIN.email));
    await expect(page).toHaveURL(/\/chat/);
    // The shell shows who is signed in and what they may see.
    await expect(page.getByText(ADMIN.email)).toBeVisible();
  });
});

test.describe("the primary flow", () => {
  /**
   * The roadmap's Definition of Done: login → ask → see citation → open trace.
   *
   * Upload is exercised in the knowledge test rather than here — ingestion is
   * asynchronous and would make this test wait on a worker, testing the queue
   * rather than the flow. The corpus is already seeded, which is the state a
   * new user actually meets.
   */
  test("ask a question, see a citation, open the trace", async ({ page }) => {
    await signIn(page, issueToken(ADMIN.tenant, ADMIN.email));

    await page.getByPlaceholder(/ask/i).fill("How long does a refund take?");
    await page.keyboard.press("Enter");

    // Generation is a real model call; give it room without giving it forever.
    const answer = page.getByText(/business days|refund/i).first();
    await expect(answer).toBeVisible({ timeout: 30_000 });

    // A cited answer is the point. An answer with no citation is exactly the
    // failure grounded generation exists to prevent.
    const citation = page.getByRole("button", { name: /^\[?1\]?/ }).first();
    await expect(citation).toBeVisible();

    // Clicking it must reveal the passage the claim came from — the user
    // reported this doing nothing visible once, which is why it is asserted.
    await citation.click();
    await expect(page.getByText(/refund/i).nth(1)).toBeVisible();

    await page.goto("/traces");
    await expect(page.getByRole("heading", { name: /traces/i })).toBeVisible();
    // Something was recorded for the request just made.
    await expect(page.getByText(/chat|retrieval|llm/i).first()).toBeVisible({
      timeout: 15_000,
    });
  });
});

test.describe("integrations", () => {
  test("an admin sees the connector list", async ({ page }) => {
    await signIn(page, issueToken(ADMIN.tenant, ADMIN.email));
    await page.getByRole("link", { name: /integrations/i }).click();

    await expect(page.getByRole("heading", { name: /integrations/i })).toBeVisible();
    await expect(page.getByText(/analytics/i).first()).toBeVisible();
  });

  /**
   * The regression test for the bug this suite was bought to catch.
   *
   * Editing sends a PATCH. PATCH requires a CORS preflight, and when the
   * allowlist lacked it the browser got a 400 and never sent the request —
   * while every curl succeeded. Saving an edit through a real browser is the
   * only assertion that covers it.
   */
  test("editing a connector saves (PATCH survives CORS preflight)", async ({ page }) => {
    await signIn(page, issueToken(ADMIN.tenant, ADMIN.email));
    await page.goto("/integrations");

    await page.getByRole("button", { name: /^edit$/i }).first().click();
    await expect(page.getByRole("dialog")).toBeVisible();

    const renamed = `Analytics warehouse ${Date.now()}`;
    const nameField = page.getByLabel(/display name/i);
    await nameField.fill(renamed);
    await page.getByRole("button", { name: /save changes/i }).click();

    // The dialog closes only on success; a 400 would leave it open with an
    // error, which is precisely what the CORS bug produced.
    await expect(page.getByRole("dialog")).not.toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(renamed)).toBeVisible();
  });

  test("the credential is never shown when editing", async ({ page }) => {
    await signIn(page, issueToken(ADMIN.tenant, ADMIN.email));
    await page.goto("/integrations");
    await page.getByRole("button", { name: /^edit$/i }).first().click();

    // Empty, with a note that the stored one is kept. A pre-filled password
    // box would mean the API returned it, which it must never do.
    const credential = page.getByLabel(/replace credential/i);
    await expect(credential).toHaveValue("");
    await expect(page.getByText(/cannot be displayed/i)).toBeVisible();
  });

  test("a reader is refused the integrations page", async ({ page }) => {
    await signIn(page, issueToken(READER.tenant, READER.email));
    await page.goto("/integrations");

    // The API refuses; the page reports it. The nav link stays visible on
    // purpose — hiding it would be a courtesy that reads like a control.
    await expect(page.getByText(/requires the admin role/i)).toBeVisible();
  });
});

test.describe("users", () => {
  test("an admin sees users with their labels", async ({ page }) => {
    await signIn(page, issueToken(ADMIN.tenant, ADMIN.email));
    await page.getByRole("link", { name: /users/i }).click();

    await expect(page.getByRole("heading", { name: /users/i })).toBeVisible();
    await expect(page.getByText(READER.email)).toBeVisible();
  });

  test("an admin cannot delete their own account", async ({ page }) => {
    await signIn(page, issueToken(ADMIN.tenant, ADMIN.email));
    await page.goto("/users");

    const ownRow = page.getByRole("listitem").filter({ hasText: ADMIN.email });
    const deleteButton = ownRow.getByRole("button", { name: /delete/i });
    // Disabled with a reason. The API refuses it too — that is what makes it a
    // control rather than a courtesy.
    await expect(deleteButton).toBeDisabled();
  });
});

test.describe("the agent", () => {
  test("a two-source question shows both tool calls", async ({ page }) => {
    await signIn(page, issueToken(ADMIN.tenant, ADMIN.email));
    await page.goto("/agent");

    await page
      .getByPlaceholder(/ask something/i)
      .fill("How many orders were shipped, and what does the handbook say about shipping?");
    await page.keyboard.press("Enter");

    // The tool timeline is the substance: an answer assembled from two sources
    // is only checkable if you can see which sources.
    await expect(page.getByText("search_knowledge").first()).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByText(/query_database|database/i).first()).toBeVisible();
  });
});

test.describe("accessibility", () => {
  /**
   * The roadmap asks for an accessibility check on the primary flows. This is
   * the floor, not an audit: every page reachable from the nav must have one
   * H1, and every control a person tabs to must have an accessible name.
   *
   * A screen reader announcing "button" six times is unusable, and that is the
   * failure this catches cheaply.
   */
  // Every page reachable from the nav. Kept in sync with NAV in app-shell.tsx
  // by test_the_accessibility_list_covers_every_nav_page below — a page added
  // to the nav and forgotten here would go unchecked, which is how /approvals
  // was missed when it shipped.
  for (const path of [
    "/chat",
    "/agent",
    "/knowledge",
    "/integrations",
    "/approvals",
    "/users",
    "/traces",
  ]) {
    test(`${path} has one heading and named controls`, async ({ page }) => {
      await signIn(page, issueToken(ADMIN.tenant, ADMIN.email));
      await page.goto(path);

      await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);

      const buttons = await page.getByRole("button").all();
      for (const button of buttons) {
        if (!(await button.isVisible())) continue;
        const name =
          (await button.getAttribute("aria-label")) ??
          (await button.textContent()) ??
          "";
        expect(name.trim(), "a visible button has no accessible name").not.toBe("");
      }
    });
  }

  test("the list above covers every page in the nav", async ({ page }) => {
    /**
     * The accessibility loop is a hardcoded list, and a hardcoded list drifts.
     * /approvals shipped without a check here because nobody remembered to add
     * it — so this reads the real nav and asserts the loop covers all of it.
     */
    await signIn(page, issueToken(ADMIN.tenant, ADMIN.email));

    const navLinks = await page
      .getByRole("navigation", { name: /main/i })
      .getByRole("link")
      .all();

    const hrefs = await Promise.all(
      navLinks.map((link) => link.getAttribute("href")),
    );

    const checked = [
      "/chat",
      "/agent",
      "/knowledge",
      "/integrations",
      "/approvals",
      "/users",
      "/traces",
    ];

    for (const href of hrefs) {
      expect(
        checked,
        `${href} is in the nav but has no accessibility check`,
      ).toContain(href);
    }
  });
});
