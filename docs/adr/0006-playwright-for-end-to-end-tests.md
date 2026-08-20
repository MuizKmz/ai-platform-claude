# ADR 0006 — Playwright for end-to-end tests

**Status:** Accepted
**Date:** 2026-08-20
**Phase:** 6

## Context

Phase 6's Definition of Done includes a test the other 357 cannot express:

> Playwright: login → upload → ask → see citation → open trace

Every existing test proves a piece. `pytest` proves the API answers correctly;
`npm run build` proves the frontend compiles and type-checks. Neither proves the
two halves meet, and that gap is not hypothetical here — it has already cost us
twice in this phase:

- **PATCH was missing from the CORS allowlist.** Every curl against the endpoint
  returned 200 because curl sends no preflight. The browser's `OPTIONS` got a
  400, the real request was never sent, and the feature was broken *only* in a
  browser. Found by the user clicking Save, not by any test.

- **A `NaN` port** would have serialised as a bare `NaN` literal — invalid JSON —
  producing a 400 with no useful message. Found by reading the form while
  investigating the first bug.

Both are browser-only failures. A test that drives a real browser is the only
kind that could have caught either.

The roadmap also asks for an accessibility check on the primary flows, which
wants the same harness.

## Decision

**Playwright**, as a dev-only dependency of the frontend, running the console
against a live backend and a real Postgres.

Tests live in `frontend/e2e/`. They drive Chromium, use the real API, and assert
on what a person sees.

## Alternatives considered

| Option | Why not |
|---|---|
| **Cypress** | Comparable capability. Playwright's cross-browser support is native rather than plugin-based, its auto-waiting removes most flake without explicit waits, and its trace viewer makes a CI failure diagnosable without reproducing locally. Cypress's architecture also makes multi-origin and multi-tab awkward, and OAuth in Phase 11 will want both. |
| **Selenium** | The mature option, and the most work. Explicit waits everywhere, more flake, and a driver-per-browser to keep matched to browser versions. |
| **Testing Library + jsdom** | Already possible without a new dependency, and would not have caught either bug above: jsdom does not enforce CORS and does not send preflights. It tests components, not the system — a different and complementary thing, not this. |
| **Nothing; keep testing by hand** | What we have been doing. It found the CORS bug — after it shipped, by the user hitting it. The point of the phase's DoD is that a non-technical person can complete the primary flow, and "we clicked it once" does not survive the next refactor. |

## Consequences

**Positive:**

- The seam between frontend and backend is finally covered. CORS, preflights,
  JSON serialisation, redirects, and token handling are all exercised as a
  browser exercises them.
- Failures come with a trace: DOM snapshots, network log, and console output per
  step. A CI failure is diagnosable without reproducing it locally.
- The accessibility check the roadmap asks for has a home.
- Auto-waiting means assertions retry until a timeout rather than racing, so
  these tests are far less flaky than the equivalent Selenium suite.

**Negative / accepted costs:**

- **A browser download.** `npx playwright install chromium` pulls ~150MB. It is
  cached in CI but is a real first-run cost.
- **Slower than everything else.** These tests take seconds each where a pytest
  case takes milliseconds. They are therefore few and cover flows, not branches.
- **They need the whole stack up** — Postgres, backend, frontend. That is the
  point, and it also means they cannot run in the unit-test job.
- **A shared database.** E2E tests write real rows. They use their own tenant and
  clean up after themselves, for the same reason `test_suite_safety.py` exists:
  a test suite once deleted this developer's real tenants.

## How we would remove it

The tests are plain TypeScript calling `page.goto`, `page.getByRole`, and
`expect`. Nothing in `frontend/src/` imports Playwright, and no application code
knows these tests exist — the dependency is confined to `frontend/e2e/` and two
lines of `package.json`.

Removing it means deleting that directory and the dev dependency. The application
is unaffected. Swapping to another runner would mean rewriting the assertions,
which at the size these will stay — a handful of flows — is an afternoon.
