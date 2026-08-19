# ADR NNNN — <short title>

**Status:** Proposed | Accepted | Superseded by ADR-NNNN
**Date:** YYYY-MM-DD
**Phase:** <which roadmap phase this arises in>

## Context

What forced this decision? What constraint, requirement, or problem is real *now*
— not anticipated. Cite the phase and the specific need.

## Decision

What we are doing, stated in one or two sentences, in the active voice.

## Alternatives considered

| Option | Why not |
|---|---|
| | |

## Consequences

**Positive:**

**Negative / accepted costs:**

## How we would remove it

Every external dependency must have an exit. Which interface does this sit behind,
and what would replacing it actually cost? If the answer is "rewrite the app", the
decision is wrong — put an interface in front of it first.
