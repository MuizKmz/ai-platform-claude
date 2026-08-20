# ADR 0004 — Next.js + Tailwind + shadcn/ui for the console

**Status:** Accepted
**Date:** 2026-08-20
**Phase:** 6

## Context

Phase 6 adds the first frontend. Connectors are *configured*, and doing that through
`.env` files and curl stops scaling at about two connectors — which is where we are. The
roadmap also puts the trace viewer before the agent, on the argument that debugging an
agent without one is miserable.

Both `docs/ARCHITECTURE_VISION.md` and `docs/ARCHITECTURE_REVIEW.md` already name
Next.js. The review adds the constraint that matters more than the framework: *"use it as
a pure client of the FastAPI API — do not put business logic in route handlers, or you
will have two backends."*

The open question was the component layer, since the framework was settled.

## Decision

**Next.js (App Router) + TypeScript + Tailwind CSS + shadcn/ui**, as a pure API client.

## Why shadcn/ui rather than a component library

This is the part worth explaining, because shadcn/ui is not a dependency in the usual
sense: `npx shadcn add button` **copies the component source into the repository**. There
is no package to upgrade and no theme API to fight.

That property is the reason to choose it here:

- **Customisation is editing, not overriding.** With MUI or Ant Design, making a component
  look different means working against its opinions through theme objects and specificity
  battles. With shadcn the component is our file.
- **Accessibility comes from Radix**, which handles focus management, keyboard navigation,
  and ARIA correctly. The phase's Definition of Done includes an accessibility check on
  primary flows; hand-rolled components would make that our problem.
- **No lock-in to remove.** The components are already ours, so "removing shadcn" means
  editing files we own rather than migrating off a framework.

The trade is that we own the maintenance of every component we copy, and upstream fixes
do not arrive automatically. For a control plane with perhaps twenty component types,
that is a good trade.

## Alternatives considered

| Option | Why not |
|---|---|
| **Vite + React SPA** | Genuinely simpler — an authenticated control plane needs no SSR, and the Next.js server adds a process for little benefit here. Rejected only because both existing design documents specify Next.js, and the difference is too small to justify contradicting them |
| **MUI / Ant Design** | Comprehensive and fast to start, but their visual identity is strong and customising away from it is the whole difficulty. We want a specific look |
| **Headless UI + hand-written components** | Same Radix-style benefits without the copied code, but every component's markup and styling becomes ours from zero rather than from a working starting point |
| **Server-rendered Jinja from FastAPI** | One less stack, one less process, no Node at all. Rejected because the chat UI needs token streaming and the trace viewer needs real interactivity — both are painful without a client framework |
| **SvelteKit** | Smaller and arguably more pleasant, but it contradicts both design documents and narrows the pool of people who could pick this up |

## Consequences

**Positive:**
- Components are ours to shape, which is what "good design" actually requires
- Radix primitives cover the accessibility DoD item
- Design tokens live in one CSS file, so a palette change is not a rewrite
- Both design documents are satisfied

**Negative / accepted costs:**
- A Node toolchain alongside Python: two package managers, two lockfiles, two CI paths
- We maintain copied component source, and upstream fixes are manual
- Next.js can host business logic in route handlers, which would give us two backends.
  Guarded by keeping `frontend/src/lib/api.ts` the only place that talks to FastAPI

## Visual direction

Dark, dense, technical — closer to Linear or Supabase than to a marketing site. Chosen
because the primary users read traces, SQL, and query results, and because a control
plane is somewhere people work rather than visit.

Defined as CSS custom properties in one place, so the palette is a decision that can be
revisited without touching components.

## How we would remove it

The frontend is a separate deployable that speaks only HTTP to the FastAPI API. Replacing
it means writing a different client against the same endpoints; the backend does not
change. That property is worth protecting deliberately — the moment business logic
appears in a route handler, this stops being true.
