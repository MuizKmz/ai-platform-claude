import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests, against a live stack.
 *
 * These do not mock the API. That is the entire point: the bugs this suite
 * exists to catch — a missing CORS method, a preflight rejection, a body that
 * serialises to invalid JSON — are invisible to anything that stubs the network.
 * A mocked E2E test is a slower unit test.
 *
 * Requires Postgres, the backend, and the frontend to be running. See
 * `e2e/README.md`. They are not started here: `webServer` would hide whether a
 * failure came from the app or from the harness, and the backend needs a
 * database this config has no business managing.
 */
export default defineConfig({
  testDir: "./e2e",
  // One worker. These tests share a database and a rate limiter, and parallel
  // workers would race over both — a connector test consuming another's probe
  // allowance produces a failure that has nothing to do with the code.
  workers: 1,
  fullyParallel: false,
  // Never in CI: a test that passes on retry is a flaky test, and marking it
  // green hides that. Locally a retry is convenient; in CI it is a lie.
  retries: 0,
  reporter: process.env.CI ? "github" : "list",
  timeout: 30_000,

  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    // A trace on failure is what makes a CI failure diagnosable without
    // reproducing it: DOM snapshots, network log, and console output per step.
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    // Assertions retry until this elapses, which removes most explicit waits
    // and most of the flake that comes with them.
    actionTimeout: 10_000,
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
