# ADR 0005 — LangGraph for agent orchestration

**Status:** Accepted
**Date:** 2026-08-20
**Phase:** 7

## Context

The architecture review rejected LangGraph in Phase 1 with a specific argument:

> *"LangGraph 1.2.11 is now a stable 1.x and it is the right choice **when you have a
> real loop with durability, interrupts and checkpointing**. In Phase 1 you have a single
> function call. Adding a graph framework to a straight line is pure cost."*

That condition is now met, and it is worth being precise about how:

- **A real loop.** Three distinct tool types exist and work standalone — knowledge
  retrieval, read-only SQL, and a GET-only REST connector. Choosing between them, then
  deciding whether the result answered the question, is genuinely iterative.
- **Durability matters.** A run makes several model calls and several upstream requests
  over tens of seconds. A worker restart mid-run currently loses all of it, including the
  money already spent.
- **Interrupts are coming.** Phase 9 adds write operations behind human approval, which is
  exactly the pause-and-resume shape LangGraph's interrupt mechanism exists for. Building
  the loop by hand now would mean rebuilding it then.

## Decision

Use **LangGraph 1.1.x** for the agent graph, with **`PostgresSaver`** for checkpointing.

The graph is: `plan → act → observe → (loop or answer)`.

## Alternatives considered

| Option | Why not |
|---|---|
| **A hand-written `while` loop** | Genuinely viable, and roughly 150 lines. Rejected on the durability requirement: a checkpointer that survives a restart means serialising state, versioning that format, and handling partial writes — which is most of what LangGraph already does. Phase 9's approval interrupts would need the rest of it |
| **LangChain agents (AgentExecutor)** | The older abstraction, with far less control over the loop and no first-class checkpointing. LangGraph is the successor from the same authors |
| **OpenAI's Assistants API** | Server-side state, so runs live at a provider rather than in a database we own. That contradicts the platform's premise: tenant data and run history sit behind our Row-Level Security or they are not governed |
| **CrewAI / AutoGen** | Multi-agent frameworks. The roadmap explicitly forbids multi-agent systems in this phase, and both bring an orchestration model far larger than one graph needs |

## Consequences

**Positive:**
- Checkpointing is a configuration choice rather than a subsystem to write
- Phase 9's approval flow maps onto `interrupt()` rather than a bespoke pause mechanism
- The graph is inspectable: nodes and edges are data, so the trace can name real steps

**Negative / accepted costs:**
- A significant dependency tree — `langchain-core` arrives with it, and so does an
  ecosystem with a history of reorganisation
- Checkpoint state is LangGraph's format in our database. Migrating away means either
  discarding run history or writing a translation
- The framework's abstractions leak into `agent/`. Contained there deliberately: nothing
  in `tools/`, `connectors/`, or `knowledge/` imports LangGraph

**Deliberately not adopted:** LangSmith, LangChain's model wrappers, its retrievers, and
its prompt templates. We have our own `LLMProvider`, our own retrieval, and our own
prompts in versioned files. Using LangGraph for graph execution alone keeps the surface
small enough to replace.

## Checkpoint storage

`PostgresSaver` writes to the platform database, in its own tables.

They carry `thread_id` rather than `tenant_id`, so **Row-Level Security does not apply to
them**. That is a real gap and it is handled at the boundary instead: thread ids are
server-generated per run and never accepted from a request, and the agent API resolves a
run's ownership from our own `agent_run` table — which does carry `tenant_id` and does have
a policy — before touching a checkpoint.

Worth stating plainly because it is the one place in this system where isolation is not
enforced by the database.

## How we would remove it

`agent/graph.py` is the only file that imports LangGraph. The nodes it wires together —
planning, tool invocation, answering — are ordinary functions taking and returning our own
`AgentState`. Replacing the framework means rewriting the wiring in that one file and
supplying a checkpointer; the tools, the limits, the authorization, and the API above it
do not move.

The property to preserve in any replacement: **hard limits are checked by our code, not by
the framework's recursion cap.** A framework's own ceiling protects the framework, not the
budget.
