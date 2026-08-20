# ADR 0007 — MCP implemented directly, not via the SDK

**Status:** Accepted
**Date:** 2026-08-20
**Phase:** 10

## Context

Phase 10 exposes this platform's read-only tools over the Model Context Protocol
so Claude Desktop, IDEs, and other agents can use them. The roadmap's constraint
is the interesting part:

> Tools exposed via MCP go through the **same** authorization path as internal
> tools — one chokepoint, two front doors.

Everything Phase 7 built rests on `ToolRegistry.invoke` being the only way a tool
runs, with authorization re-checked at every invocation against a verified
`Principal`. An MCP front door that authorizes differently would be a second
security model, and two security models is one more than can be kept correct.

The spec also requires OAuth 2.1 resource-server behaviour: a token must be
issued **specifically for this server** (RFC 8707 audience), and the server
**MUST NOT accept or transit any other tokens**. That prohibition is the
confused-deputy defence, and it is not optional.

The question was whether to take the official `mcp` Python SDK.

## Decision

**Implement the MCP server directly** — JSON-RPC 2.0 over HTTP, on our own
FastAPI router, using our own `Principal` and `ToolRegistry`.

No new dependency.

## Why not the SDK

Three reasons, in order of weight.

**1. The SDK owns the request lifecycle; we need to own authorization.**

The SDK's server abstractions expect to manage sessions and dispatch tool calls
themselves. Getting our `Principal` — derived from a verified JWT, carrying
tenant and labels — into that dispatch means adapting around the framework at
exactly the point where the phase's guarantee lives. Writing the dispatch
ourselves makes the chokepoint literal: the MCP handler calls
`registry.invoke(principal, name, **args)`, the same line the agent calls.

**2. The protocol surface we need is small.**

Four JSON-RPC methods — `initialize`, `tools/list`, `tools/call`, `ping` — plus
the RFC 9728 metadata document. That is a few hundred lines including the error
mapping. The SDK is worth its weight when you want the full protocol: sampling,
elicitation, resources, prompts, notifications. The roadmap explicitly defers all
of those (*"No MCP sampling/elicitation"*), and a dependency taken for features
you have decided not to build is a dependency taken for nothing.

**3. Auth is the part that must not be someone else's default.**

Audience validation and the no-passthrough rule are the security properties of
this phase. Implementing them in our own `mcp/auth.py`, next to a test that
asserts a token minted for another audience is refused, is better than
configuring them into a library and trusting the configuration held.

## Alternatives considered

| Option | Why not |
|---|---|
| **Official `mcp` Python SDK** | See above. The lifecycle it owns is the lifecycle our authorization needs to sit inside, and we would use perhaps a fifth of it. |
| **`fastapi-mcp` or similar auto-exposers** | These generate MCP tools from existing endpoints. That is the opposite of what this phase wants: it would expose the API surface rather than the *tool* surface, bypassing `ToolRegistry` entirely and taking write endpoints with it. |
| **stdio transport instead of HTTP** | Simpler, and wrong here. stdio means a local subprocess with no independent identity, so per-client authorization has nothing to attach to. HTTP with a bearer token is what makes "this client, this tenant, these labels" expressible. |
| **Not exposing MCP at all** | Defensible, and it is what Phases 0–9 did. But the roadmap is right that this is a real product capability: it is the difference between a platform people query through our console and one their existing tools can reach. |

## Consequences

**Positive:**

- One authorization path. `registry.invoke` is called identically by the agent
  and by the MCP handler, and `test_mcp_tools_respect_authorization` asserts the
  denials match.
- Audience validation and no-passthrough are ours, tested directly rather than
  configured.
- Write tools are excluded from the MCP surface by an explicit filter, not by
  hoping a framework does not enumerate them.
- No dependency to track, audit, or upgrade — and `pip-audit` already found four
  CVEs in a transitively-pulled package this month.

**Negative / accepted costs:**

- **We track the spec ourselves.** MCP is young and moving; a revision that adds
  a required method means editing our handler. Mitigated by the surface being
  small and by the version being pinned in `initialize`.
- **No protocol conformance suite.** The SDK would come with the maintainers'
  own idea of correct. Ours is tested against the spec text and against a real
  client, which is weaker for exotic cases and adequate for four methods.
- **Features we skip stay skipped.** Adding sampling or resources later means
  writing them, not enabling them. Given the roadmap defers all of it, that cost
  is deferred too.

## How we would remove it

`app/mcp/` is three modules behind one router. Nothing in `tools/`, `agent/`, or
`connectors/` knows MCP exists — the dependency points inward only, and the MCP
layer is a caller of `ToolRegistry`, never a thing it calls.

Replacing it with the SDK means rewriting those three modules to hand our
`Principal` into the SDK's dispatch, and deleting the router. The tools, the
authorization, and the audit trail are untouched, because none of them are MCP's.
