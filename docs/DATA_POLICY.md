# Data policy

**Status:** Current as of Phase 8
**Companion to:** [THREAT_MODEL.md](THREAT_MODEL.md) (boundary B5)

What data leaves this system, where it goes, how long we keep it, and what we
do not promise.

---

## The one thing to understand first

**Retrieved passages are sent to a third-party model provider.**

That is not a bug and it is not avoidable while using a hosted model. The system
answers questions by finding relevant passages and giving them to a model to
read. A model cannot answer from a passage it was not given.

So: if a document is retrievable by someone, its relevant paragraphs will reach
OpenAI when that person asks a question they answer. Everything below is about
bounding what that means, not about pretending otherwise.

---

## What leaves, by destination

### To the model provider (OpenAI, today)

| Sent | When | Bounded by |
|---|---|---|
| The user's question | Every chat or agent request | — |
| Retrieved chunk text | Every request that finds sources | Top-k passages, never a whole corpus |
| Chunk text for embedding | At ingestion, once per chunk | The document being ingested |
| Table and column names | Only when generating SQL | The curated schema — never raw tables |
| Prior turns in a conversation | Multi-turn chat | The conversation's own history |

### Never sent, and enforced

| Never sent | Why it cannot be |
|---|---|
| Connector credentials | Passed directly to the connector, never placed in a prompt |
| The JWT signing key | Never leaves configuration |
| Another tenant's data | Not retrievable, so not available to send |
| Data outside the caller's labels | Filtered before ranking, so never in the candidate set |
| Whole documents | Only matching chunks are retrieved |
| Raw source-system tables | The semantic layer exposes curated views only |

The distinction matters: the second table is not a promise of restraint, it is
a description of what is architecturally unreachable at the point of sending.

### To nowhere else

No analytics vendor, no telemetry service, no error-reporting SaaS. Traces go
to our own Postgres. That was a deliberate choice in Phase 1 — the durable
decision is emitting spans at all; where they ship is swappable later, and today
they ship nowhere.

---

## Data classes

| Class | Examples | May reach the provider? | Notes |
|---|---|---|---|
| **Public** | Handbooks, published policy | Yes | The default case |
| **Internal** | Procedures, org charts | Yes, if retrieved | Label controls who can retrieve |
| **Confidential** | Contracts, finance, HR | Yes, if retrieved | Label it tightly; see below |
| **Regulated** | Health, payment card, national ID | **Should not be ingested** | See *Regulated data* |
| **Credentials** | Passwords, API keys, tokens | **Never** | Architecturally unreachable |

### Regulated data

The platform does **not** currently meet the handling requirements of HIPAA,
PCI-DSS, or equivalent regimes. Specifically:

- there is no Business Associate Agreement with the model provider
- PII detection finds formats, not meaning, so it cannot be relied on to catch
  everything
- an operator with database access can read any tenant's data

**Do not ingest regulated data into this deployment.** If that becomes a
requirement, it is a project of its own: a self-hosted model, per-tenant
encryption keys, and a signed agreement with whoever hosts it.

The PII badge in the console exists to make this visible rather than
theoretical. A document showing **⚠ 47 PII** with 5 credit-card matches is
telling you something before you choose its label.

---

## PII: what we do and do not do

**We detect and count at ingestion. We do not block, and we do not strip.**

Blocking would refuse the product. Enterprise documents contain people — an HR
handbook names employees, a support export is nothing but customer details. A
platform that refused those would answer no useful questions.

Stripping would break retrieval. A support ticket with the customer's name
removed cannot answer "what did this customer report?".

So the platform records **counts by kind** on each document — `{"email": 4000,
"credit_card": 2}` — and shows them next to the labels in the console. The
decision the counts inform is *who may retrieve this*, which is the decision
that actually controls exposure.

**Counts, never values.** Carrying examples would put the PII into the very
metadata written to make it visible.

### Where PII is redacted

| Location | Redacted? | Why |
|---|---|---|
| Documents and chunks | No | Stripping breaks the answers |
| Trace attributes | **Yes, always** | A second copy on a different retention clock, readable by admins who may hold none of the labels that gated the original |
| Conversation history | No | It is the conversation; the user wrote it |
| Audit log SQL | No | The generated SQL is the record; it queries curated views, not raw personal data |
| Log lines | Via traces | Application logs carry counts and identifiers, not content |

Trace redaction happens in `Span.set_attribute` — the single point every
attribute passes through. Per-call-site redaction would prove today's call sites
are careful and say nothing about the ones added next phase.

### What the detector catches

Formats: email addresses, phone numbers, Luhn-valid card numbers, US SSNs,
IBANs, IP addresses, and common API-key shapes.

**What it does not catch:** meaning. "The patient in room 3", a name in prose, a
medical condition described in a sentence. There is no regex for those.

One known over-report: a four-part version number (`1.2.3.4`) is
indistinguishable from an IP address by shape. It is counted as an IP. The cost
of that error is an inflated number in metadata; the cost of the reverse — a
real internal address unrecorded — is reconnaissance sitting in a corpus.

---

## Retention

Different tables, different clocks. A single period would be wrong in both
directions.

| Data | Kept | Why |
|---|---|---|
| Trace spans | **30 days** | Debugging telemetry. Nobody investigates a six-week-old latency spike, and this is the highest volume by far |
| Conversations and messages | **90 days** | The most sensitive thing here, and the least useful once the question is answered |
| Agent runs | **90 days** | A run is a conversation that used tools |
| Ingest jobs | **30 days** | Operational history; the documents produced are the durable part |
| Connector audit | **365 days** | The compliance record — who ran what SQL. The one somebody asks for a year later |
| Documents and chunks | **Until deleted** | The corpus. Removed by lifecycle rules, not by age |
| Connector config and users | **Until deleted** | Configuration, not history |

Enforced by `uv run python -m app.cli retention`. **Deletes, not archives** — an
archive is a second copy with the same exposure and none of the attention.

Scheduling it is a deployment concern; see the [runbook](RUNBOOK.md).
`test_every_time_series_table_has_a_policy` fails if a future table starts
accumulating without a decision, so "forever" cannot become the default by
omission.

---

## Encryption

| Where | How |
|---|---|
| Connector credentials at rest | Fernet, encrypted by the **application** before reaching the database |
| Everything else at rest | Whatever the storage layer provides |
| In transit to the provider | TLS |
| In transit to connectors | TLS where the target supports it |

Application-level encryption for credentials is the point worth stating: a
database dump, a replica, or a stolen backup contains ciphertext and nothing
else. Storage-level encryption at rest protects the **disk**, not the **dump**,
and those are different threats.

The key lives outside the database, in configuration. Rotating it means
re-encrypting stored credentials — see the runbook.

---

## Deleting a tenant's data

`DELETE FROM tenant WHERE id = ...` cascades to every table carrying
`tenant_id`. That is by design: the foreign key is `ON DELETE CASCADE`
everywhere, so there is no partial-deletion state to reason about.

What it does **not** reach:

- LangGraph checkpoint tables, which carry no `tenant_id`. The `agent_run` rows
  that reference them are deleted immediately; the checkpoints become orphans
  until the next retention run, which purges any whose `agent_run` is gone. A
  thread without one is unreachable — resuming it needs the ownership record —
  so nothing is lost, but there is a window.
- Anything already sent to the model provider. Their retention terms govern
  that, not ours.

---

## Choosing a different provider

The `LLMProvider` interface is four methods. A self-hosted model — vLLM, Ollama,
anything OpenAI-compatible — is a new implementation of that interface and a
configuration change. Nothing else in the system knows which provider is in use.

That is the exit if the egress position above becomes unacceptable, and it was
built in Phase 2 for exactly this reason.
