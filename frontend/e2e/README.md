# End-to-end tests

Playwright, driving Chromium against a live stack. See
[ADR 0006](../../docs/adr/0006-playwright-for-end-to-end-tests.md) for why.

## Why these exist

Every other test proves a piece. `pytest` proves the API answers correctly;
`npm run build` proves the frontend compiles. Neither proves the two halves
meet, and that gap cost us twice in Phase 6:

- **PATCH was missing from the CORS allowlist.** Every curl returned 200 —
  curl sends no preflight. The browser's `OPTIONS` got a 400, the real request
  was never sent, and the feature was broken *only* in a browser. Found by a
  person clicking Save.
- **An empty port field** would have serialised as a bare `NaN` literal —
  invalid JSON — producing a 400 with no useful message.

Writing this suite immediately found a third: the integrations form rendered
its labels without `htmlFor`, so nothing associated them with their inputs.
Playwright could not find fields by label, and neither could a screen reader.

**Never stub the network in this directory.** A mocked E2E test is a slower
unit test, and cannot see any of the above.

## Running them

All three services must be up.

```powershell
docker compose up -d                          # Postgres + Redis

cd backend                                    # terminal 2
uv run uvicorn app.main:app --reload

cd frontend                                   # terminal 3
npm run dev
```

Then:

```powershell
cd frontend
npm run e2e            # headless
npm run e2e:ui         # watch them run, step through failures
```

First run only:

```powershell
npx playwright install chromium
```

## Debugging a failure

Failures keep a trace — DOM snapshots, network log, console output, per step:

```powershell
npx playwright show-trace test-results/<test-name>/trace.zip
```

The network tab there is usually the fastest route to a CORS or serialisation
problem, since it shows the preflight as its own request.

## What they assume

- **Seeded data.** The `acme` tenant with `admin@acme.test` and
  `carol@acme.test`, and a corpus that answers "How long does a refund take?".
  Tests read this data; they do not create it.
- **Tokens from the CLI.** `helpers.ts` shells out to
  `uv run python -m app.cli token`, the same command the login page tells a
  person to run. Minting tokens in TypeScript would let these pass against a
  broken CLI.
- **One worker.** They share a database and a rate limiter. Parallel workers
  would race over both, and a connector test consuming another's probe
  allowance fails for reasons unrelated to the code.

## Adding one

Assert what a person sees — a role, a label, visible text — not a CSS class.
`getByRole` and `getByLabel` fail when a control is inaccessible, which is a
feature: that is how the missing `htmlFor` surfaced.

Keep the suite small. These take seconds each where a pytest case takes
milliseconds, so they cover flows end to end and leave branches to the unit
tests.
