# Install-time schema discovery — plan

Written before any code. Prompted by a question during today's IoT deployment
work: when a new system (WMS, next) gets connected, should EAIP "learn" its
architecture once and remember it? The short answer is **no, not as
memorization** — and this document is the corrected, real version of that
idea, checked against what already exists rather than assumed.

## What already exists, and why the "learn once, remember forever" framing is wrong

`backend/app/connectors/sql/semantic_layer.py` already does real schema
discovery, today, with no model call at all:

- **`from_discovered_schema`** builds a working semantic layer purely from
  database metadata — column names and types — with deliberate guardrails
  baked into its output notes: *"Use only the approved views listed above. Do
  not guess at base tables or columns"* and *"A column exists only in the
  views that list it."* Its own docstring records a real failure this
  prevents: a model that had seen `device_name` hinted on two views assumed
  it existed on a third, wrote SQL against a column that wasn't there, and
  the query failed in front of a user.
- **`discover_value_hints`** reads the *actual distinct values* of
  enumeration-shaped columns — but only through three independent fences: an
  allowlist of column names (not a cardinality guess, since a table of six
  customers also has "few distinct values"), a hard cap that drops rather
  than truncates a column that turns out to hold real data, and a
  never-hint marker list for anything that reads as personal (`operator` is
  a comparison symbol in an alerts view and a person in a shift log).

This is the mechanism that makes "EAIP already knows the shape of a new
connector" true **today**, and it is re-derived from the live schema on every
question, not memorized once at install and trusted forever. That is
deliberate, not a gap: a schema changes — a column gets renamed, a table
deprecated — and a model that "remembers" the old shape would give a
confident, wrong answer, which is worse than an error. `v_metric_catalogue`
in the IoT curated views exists for the same reason: it tracks which metrics
*stopped* being collected, so the agent doesn't confidently answer from dead
data it memorized once.

**So the real gap is not discovery — it's turning discovery into curated
views a human approves**, the step `iot-curated.sql` did by hand today. That
step is what this document actually plans.

## What the wizard is

A guided flow, run once per new system integration, that produces the same
kind of artifact `infra/mysql/iot-curated.sql` is — a curated-views SQL file
and matching semantic-layer entries — faster, and with a first draft the
admin edits instead of writes from scratch.

```
1. Admin points the wizard at the new system's database
   (read-only credentials the system's own admin provides —
   same trust boundary as any connector today, nothing new)

2. Wizard runs the EXISTING discovery primitives against it:
     - table/column introspection (already exists)
     - from_discovered_schema() -> a baseline semantic layer,
       no model call, exactly as it works today
     - discover_value_hints() -> safe enum vocabularies,
       same three fences as today, no model call

3. ONE model call, here only: given the baseline semantic layer,
   draft a proposed curated-views SQL file (which tables to expose
   as which views, columns to omit, docstring-style comments
   explaining each — the same style iot-curated.sql already has)
   and proposed column descriptions to improve on the mechanical
   defaults from_discovered_schema() produces.

4. Admin reviews the draft in a UI: edit any view, remove a
   proposed column, rewrite a description, reject the whole
   thing and start over. NOTHING is created until approved --
   same "admin approves before it's real" pattern Phase 9's
   write-tool approval flow already establishes elsewhere in
   this project.

5. On approval: the real SQL runs (creates the curated database,
   the views, the scoped read-only role -- exactly what Step 1
   of infra/DEPLOY-aapanel.md did by hand for IoT), and the
   semantic layer entries are persisted.

6. From this point on, nothing is different from how IoT works
   today: the agent reads the curated views and semantic layer
   FRESH on every question. Nothing was "taught" to the model in
   a way it recalls without checking -- step 3's model call only
   ever produced a draft a human then owns.
```

## Where the one model call fits, and why it's safe to be narrow

The call in step 3 never touches real data rows (`discover_value_hints`
already fenced that separately, before this call happens) and never becomes
something the agent "remembers" — its entire output is a **draft config
file**, reviewed by a human, then thrown away. The artifact that persists is
the SQL and the semantic layer entries, both auditable, both editable by hand
afterward exactly like `iot-curated.sql` is today. This is spending tokens
once to draft a document faster, not building a new kind of memory.

## Relationship to the chat widget SDK plan

Independent pieces. [CHAT_WIDGET_SDK.md](CHAT_WIDGET_SDK.md) is about how a
system's own frontend gets a chat surface talking to EAIP. This document is
about how a new system's *data* gets connected to EAIP in the first place —
a precondition for that chat surface having anything useful to say about
WMS-shaped questions. Build order isn't fixed by dependency: the widget can
ship against IoT's already-existing curated views today; this wizard matters
whenever the *next* system (WMS) needs connecting, whichever comes first.

## Explicitly out of scope for this plan

- **Which system is next.** This document describes the mechanism, not a
  commitment to build it for WMS specifically before anything else.
- **Automatic approval / no human in the loop.** Every version of this
  considered keeps step 4. An unreviewed model-drafted view definition
  running against a real production database is not a feature this project
  should ship, per the same reasoning that keeps write tools behind
  approval (Phase 9) and keeps retrieval authorization-filtered before
  ranking rather than after (security invariant #3) — a draft that looks
  plausible is not the same guarantee as one a person checked.
- **Changing `from_discovered_schema` or `discover_value_hints`.** Both are
  reused as-is. This plan is a workflow and a UI around them, not a rewrite.

## Status

**Not yet built.** Written to capture the correct shape of the idea before
any code, the same way `CHAT_WIDGET_SDK.md` was.
