"""Translating between MCP's tool shape and ours.

This module is deliberately thin, and the thinness is the design. It converts a
`ToolSpec` into MCP's JSON Schema form and converts a `ToolResult` back — and
that is all. It makes no authorization decision, because there is exactly one
place that does.

**Write tools are excluded here, by filter.** The roadmap requires it and the
reasoning is worth stating: an MCP client is not our code and not our console.
It has no way to show a payload to a human, no approval queue, and no way for a
person to reject an action. Exposing a tool that creates approval requests to a
client that cannot complete the flow produces a queue of proposals nobody
initiated a review for.

The exclusion is by TYPE, not by name. A write tool added in a later phase is
excluded automatically, because a filter listing names is a filter someone
forgets to update.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.security import Principal
from app.tools.base import ToolRegistry, ToolSpec
from app.tools.write_base import WriteTool


def visible_tools(registry: ToolRegistry, principal: Principal) -> list[ToolSpec]:
    """Tools this MCP client may see: authorized, and read-only.

    Two filters, applied in this order because they answer different questions.
    `available_to` answers "may this principal use it" — the same check the
    agent's prompt builder uses. The write filter answers "should this surface
    offer it at all", which is a property of the surface rather than the caller.
    """
    authorized = registry.available_to(principal)
    return [
        spec for spec in authorized if not _is_write_tool(registry, principal.tenant_id, spec.name)
    ]


def _is_write_tool(registry: ToolRegistry, tenant_id: uuid.UUID, name: str) -> bool:
    """Whether a registered tool is a write tool.

    Checked by type rather than by a list of names. A write tool added next
    phase is excluded without anyone remembering to add it here — and a filter
    that depends on remembering is the filter that eventually leaks.
    """
    return isinstance(registry.get_registered(tenant_id, name), WriteTool)


def spec_to_mcp(spec: ToolSpec) -> dict[str, Any]:
    """A ToolSpec as MCP describes a tool.

    Our parameters are a flat name → description map, which converts to a JSON
    Schema of all-string properties. That is lossier than a real schema and
    deliberately so: `ToolSpec.parameters` is flat because nested schemas
    produce nested mistakes, and MCP is not a reason to change the internal
    shape.
    """
    return {
        "name": spec.name,
        "description": spec.description,
        "inputSchema": {
            "type": "object",
            "properties": {
                param: {"type": "string", "description": description}
                for param, description in spec.parameters.items()
            },
            # Nothing is required. A tool that needs an argument validates it
            # and returns a readable error, which is a better experience than a
            # protocol-level rejection with no explanation.
            "required": [],
        },
    }


def result_to_mcp(content: str, *, is_error: bool = False) -> dict[str, Any]:
    """A ToolResult as MCP returns tool output.

    An error becomes `isError: true` with the message as content, rather than a
    JSON-RPC error. That is what the spec intends: a tool that failed is a tool
    result, and the model should see why. A protocol error would hide it.
    """
    return {
        "content": [{"type": "text", "text": content}],
        "isError": is_error,
    }
