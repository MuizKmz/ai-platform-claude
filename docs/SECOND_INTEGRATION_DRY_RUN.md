# A second integration, done cold — plan

Written before any code or config. The goal: prove the *process* of
connecting a new system to EAIP, not re-prove that IoT's data works — that's
already proven, repeatedly, this session. IoT is being reused only as a
convenient, already-known data source; everything built here is new, parallel,
and named so it never collides with the real, currently-working IoT
integration.

**Two parts, deliberately sequenced hard-then-easy:**

1. **The manual dry-run** — walk the real, current onboarding process
   end-to-end, by hand, as if this were a genuinely new integration with no
   prior EAIP knowledge. The point is to find where the documented process is
   unclear, slow, or wrong — not to build something new.
2. **The schema-discovery wizard** — build what
   [SCHEMA_DISCOVERY_WIZARD.md](SCHEMA_DISCOVERY_WIZARD.md) already planned,
   now informed by exactly what part 1 showed is tedious or error-prone to do
   by hand, rather than guessing what to automate in advance.

## Why simulate rather than reuse the real IoT integration

The real `iot_curated` views, `eaip-mcp`'s IoT scoping, and the "IoT Platform"
connector row were all built across many sessions by people who already knew
the schema, the platform, and each other's context. That is not what a
genuinely new integration looks like, and it means we have never actually
tested whether the *documented* process — the thing a stranger would follow —
holds up on its own. This dry-run is that test.

**Nothing about the real, working IoT integration changes.** Everything below
is built under new names, in parallel. The real `iot_curated` database, the
real `eaip-mcp` client's `tenant_id`/`labels`, the real connector row, and the
widget demo already proven working all keep running exactly as they are.

## Part 1 — the manual dry-run

Followed as written, by someone deliberately not skipping ahead on
memory — reading each doc's actual current text, not what we remember it
saying.

| Step | Follow | Produces |
|---|---|---|
| 1 | `infra/DEPLOY-aapanel.md` Step 1's *pattern* (not IoT's specific SQL) | A second curated-views database against the SAME underlying `iot_db` schema, named distinctly — e.g. `iot_curated_dryrun` — with its own scoped read-only user, e.g. `eaip_readonly_dryrun` |
| 2 | `infra/keycloak/MCP-SETUP.md` | A second Keycloak client (not `eaip-mcp` — e.g. `eaip-mcp-dryrun`), its own service-account `tenant_id`/labels |
| 3 | The console's own connector-creation flow (Integrations → Add) | A second connector row, pointed at `iot_curated_dryrun`, its own slug/labels |
| 4 | `sdk/SETUP.md` | A second widget/demo instance (or reuse the same demo app, repointed at the new client's credentials) actually answering a question through the new connector |

**What "done" looks like:** the same proof already established for the real
IoT integration — a real token, a real `tools/list`, a real grounded answer,
DevTools confirming no browser-to-EAIP calls — but reached by following the
docs literally, from zero, timing it and noting every point of friction.

**What this step is actually for:** a running list of concrete doc problems —
a step that assumed context a first-timer wouldn't have, a command that
needed a fix not yet written down, a place the docs disagreed with what the
live system actually does. That list is the real deliverable of Part 1, not
the dry-run connector itself, which gets deleted once Part 1 is done (see
cleanup, below).

## Part 2 — the schema-discovery wizard

Built per [SCHEMA_DISCOVERY_WIZARD.md](SCHEMA_DISCOVERY_WIZARD.md)'s design,
unchanged in its safety shape (the one model call only ever drafts a
config a human reviews; nothing is created without approval) — but now
scoped by Part 1's friction list rather than guessed upfront. Concretely,
Part 1 is expected to answer questions the original plan left open:

- Which of Part 1's manual steps took the most real time, and is that the
  one worth automating first, rather than assuming "the SQL-writing step" is
  automatically the bottleneck?
- Did Part 1 hit a Keycloak or connector-creation step that a wizard
  *couldn't* safely automate (an access decision that has to stay a human
  clicking a button, per the plan's own explicitly-out-of-scope list)? If so,
  the wizard's boundary is exactly there, confirmed rather than assumed.
- Does the semantic-layer draft `from_discovered_schema` produces (already
  real, already running — see the linked plan) actually look usable as a
  starting point for a *second*, differently-shaped schema, or did IoT's
  schema happen to be unusually clean?

Part 2's own definition of done, unchanged from the linked plan: one real
curated-views set, produced by the wizard, reviewed and approved by a human
before anything is created — the same approve-before-real-effect gate as
Phase 9's write tools.

## Cleanup, after Part 1

The dry-run connector, Keycloak client, and curated-views database are
throwaway — delete them once Part 1's friction list is written down.
Keeping a second, permanently-idle IoT-shaped connector around indefinitely
is exactly the kind of thing this project's own IoT curated-views script
warns against elsewhere: an unused, forgotten grant is a liability, not a
convenience.

```sql
-- Mirrors iot-curated.sql's own "to undo everything" block.
DROP DATABASE iot_curated_dryrun;
DROP USER 'eaip_readonly_dryrun'@'localhost';
```

Plus, in Keycloak: disable/delete the `eaip-mcp-dryrun` client. Plus, in the
console: delete the dry-run connector row.

## Status

**Not yet started.** This document is the plan; Part 1 hasn't been run and
Part 2 hasn't been built.
