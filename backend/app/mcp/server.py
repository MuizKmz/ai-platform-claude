"""The MCP server: a second front door onto the same tools.

JSON-RPC 2.0 over HTTP. Four methods — `initialize`, `tools/list`, `tools/call`,
`ping` — and the RFC 9728 metadata document.

**One chokepoint, two front doors.** The line that matters in this module is

    result = registry.invoke(principal, name, **arguments)

which is the same call the agent makes. Not a copy of it, not an equivalent
check — the same function, deciding the same way, against a Principal derived
the same way. `test_mcp_tools_respect_authorization` asserts the denials match.

**Read-only.** Write tools are filtered out of `tools/list` and refused by
`tools/call`, because an MCP client has no approval queue and no way to show a
payload to a human. See `tool_bridge`.

**The token stops here.** It is verified, used to derive a Principal, and
dropped. Nothing outbound carries it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import enforce_llm_limits
from app.core.quota import record_spend
from app.core.security import AuthError, Principal
from app.db.session import SessionLocal
from app.mcp.auth import (
    mcp_audience,
    principal_from_mcp_token,
    protected_resource_metadata,
)
from app.mcp.tool_bridge import result_to_mcp, spec_to_mcp, visible_tools
from app.tools.base import ToolAuthorizationError, ToolError, ToolRegistry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])

# The spec revision this server implements. Pinned rather than echoed back from
# the client's request: agreeing to whatever version a client claims is how you
# end up accepting a shape you do not implement.
PROTOCOL_VERSION = "2026-07-28"

SERVER_INFO = {"name": "eaip", "version": "1.0.0"}

# What one tool call is assumed to cost, for budgeting. Roughly an embedding
# request plus overhead; deliberately a little high, because under-counting a
# budget control is the failure that matters.
ESTIMATED_TOOL_CALL_USD = 0.0002

# JSON-RPC error codes. The last is ours; the rest are from the spec.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHORIZED = -32001
RATE_LIMITED = -32002
OVER_BUDGET = -32003


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """A JSON-RPC error. Messages here are deliberately uninformative about
    authorization: the same rule as everywhere else in this system."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


@router.get("/.well-known/oauth-protected-resource")
def resource_metadata() -> dict[str, object]:
    """RFC 9728. How a client discovers what token to ask for.

    Unauthenticated by necessity — a client reads this precisely because it does
    not yet have a token. It discloses no tenant data: an issuer URL and an
    audience string.
    """
    return protected_resource_metadata()


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """The JSON-RPC entry point.

    Auth is checked before the body is dispatched, and `initialize` is the only
    method exempt — a client must be able to negotiate before it has decided
    which token to fetch. It returns no tenant data.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — any malformed body is one error
        return JSONResponse(_error(None, PARSE_ERROR, "Parse error"), status_code=400)

    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return JSONResponse(_error(None, INVALID_REQUEST, "Invalid Request"), status_code=400)

    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method == "initialize":
        return JSONResponse(
            _result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    # Only what we implement. Advertising `resources` or
                    # `prompts` we do not serve invites clients to call them.
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                },
            )
        )

    if method == "ping":
        return JSONResponse(_result(request_id, {}))

    principal = _authenticate(authorization)
    if principal is None:
        response = JSONResponse(_error(request_id, UNAUTHORIZED, "Unauthorized"), status_code=401)
        # Point an unauthenticated client at the metadata document, as RFC 9728
        # prescribes. Without this a client that lacks a token has no way to
        # learn which audience to request.
        response.headers["WWW-Authenticate"] = (
            f'Bearer resource_metadata="/.well-known/oauth-protected-resource", '
            f'audience="{mcp_audience()}"'
        )
        return response

    if method == "tools/list":
        return JSONResponse(_result(request_id, _list_tools(principal)))

    if method == "tools/call":
        # Rate limit and daily budget, per tenant — the same controls the chat
        # and agent endpoints carry.
        #
        # Found by probing: three MCP calls made real embedding requests and
        # moved the quota by zero. An MCP client is a machine, it will call in
        # a loop, and an unmetered front door onto metered tools makes the
        # budget a setting rather than a control. Same class of hole as the
        # streaming chat path in Phase 8.
        try:
            enforce_llm_limits(principal)
        except HTTPException as exc:
            code = RATE_LIMITED if exc.status_code == 429 else OVER_BUDGET
            response = JSONResponse(
                _error(request_id, code, str(exc.detail)), status_code=exc.status_code
            )
            for header, value in (exc.headers or {}).items():
                response.headers[header] = value
            return response

        result = _call_tool(principal, params)

        # Record what the call cost, so an MCP client draws down the same budget
        # everything else does.
        #
        # An ESTIMATE, and labelled as one. Tools do not report their cost — the
        # embedding provider does not price itself — and plumbing accounting
        # through every tool is a larger change than this belongs in. A flat
        # per-call figure is wrong in the third decimal and right about the
        # thing that matters: a client calling in a loop exhausts the budget
        # rather than running free.
        record_spend(principal.tenant_id, ESTIMATED_TOOL_CALL_USD)

        return JSONResponse(_result(request_id, result))

    return JSONResponse(
        _error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}"),
        status_code=404,
    )


def _authenticate(authorization: str | None) -> Principal | None:
    """Derive a Principal from the bearer token, or None.

    The token is used here and nowhere else. It is not stored on the Principal,
    not passed to a tool, and not attached to any outbound request — see
    `auth.py` on why passthrough is forbidden.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        return principal_from_mcp_token(authorization[7:].strip())
    except AuthError:
        # Logged without the token and without the reason. A response that
        # distinguished "wrong audience" from "bad signature" would be an
        # oracle for what this server accepts.
        logger.info("mcp authentication failed")
        return None


def _list_tools(principal: Principal) -> dict[str, Any]:
    """Tools this client may use. Read-only, and label-filtered."""
    with SessionLocal() as session:
        _bind_tenant(session, principal)
        registry = _build_registry(session, principal)
        specs = visible_tools(registry, principal)

    return {"tools": [spec_to_mcp(spec) for spec in specs]}


def _call_tool(principal: Principal, params: dict[str, Any]) -> dict[str, Any]:
    """Invoke a tool through the same path the agent uses.

    Errors become tool results rather than JSON-RPC errors: a tool that failed
    is a result the model should see, and a protocol error would hide the
    reason.
    """
    name = str(params.get("name") or "")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return result_to_mcp("Arguments must be an object.", is_error=True)

    with SessionLocal() as session:
        _bind_tenant(session, principal)
        registry = _build_registry(session, principal)

        # Write tools are refused here as well as hidden from tools/list. A
        # client that learned a name from somewhere else still cannot call it.
        if name not in {spec.name for spec in visible_tools(registry, principal)}:
            logger.info(
                "mcp tool refused: tool=%s tenant=%s client=%s",
                name,
                principal.tenant_id,
                principal.email,
            )
            return result_to_mcp(f"You do not have access to {name!r}.", is_error=True)

        # Only the parameters the tool declares. An MCP client is not our code,
        # and forwarding whatever it sent means a tool's signature is the last
        # thing standing between arbitrary keys and `run(**kwargs)`.
        #
        # Nothing was exploitable — labels and tenant come from the Principal,
        # never from arguments — but a probe sending `__proto__` and `labels`
        # reached the tool, and a filter at the boundary is better than a
        # signature that happens to reject them.
        spec = next(s for s in visible_tools(registry, principal) if s.name == name)
        filtered = {k: v for k, v in arguments.items() if k in spec.parameters}
        dropped = sorted(set(arguments) - set(filtered))
        if dropped:
            logger.info(
                "mcp dropped undeclared arguments: tool=%s keys=%s tenant=%s",
                name,
                dropped,
                principal.tenant_id,
            )

        try:
            # THE line. Identical to the agent's call, against a Principal
            # derived the same way.
            result = registry.invoke(principal, name, **filtered)
        except ToolAuthorizationError as exc:
            return result_to_mcp(str(exc), is_error=True)
        except ToolError as exc:
            return result_to_mcp(str(exc), is_error=True)
        except TypeError:
            # A bad argument name. Readable, because it is the caller's mistake
            # and they can fix it.
            return result_to_mcp(
                f"Wrong arguments for {name!r}. Check the tool's input schema.",
                is_error=True,
            )

        session.commit()

    logger.info(
        "mcp tool invoked: tool=%s tenant=%s client=%s failed=%s",
        name,
        principal.tenant_id,
        principal.email,
        result.failed,
    )

    return result_to_mcp(result.content, is_error=result.failed)


def _bind_tenant(session: Session, principal: Principal) -> None:
    """Set the tenant context RLS reads.

    The MCP handler manages its own session rather than using the request
    dependency, so this must be done explicitly — a session without it returns
    zero rows, which is the safe failure but a confusing one.
    """
    from sqlalchemy import text

    session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(principal.tenant_id)},
    )


def _build_registry(session: Session, principal: Principal) -> ToolRegistry:
    """The same tools the agent gets, built the same way.

    Imported here rather than at module scope to keep the layering honest: mcp/
    depends on api/, and a top-level import would make that a cycle the moment
    api/ wanted anything from mcp/.
    """
    from app.api.v1.agent import build_registry

    return build_registry(session, principal, _NullLLM())


class _NullLLM:
    """A placeholder for the registry builder's LLM parameter.

    The SQL tool takes an LLM to turn a question into SQL. Over MCP the CLIENT
    is the model — it sends a question, and generating SQL for it would mean
    paying for a second model to interpret the first one's request. So the
    query tool is offered without a working generator and refuses at invocation
    with a clear message, rather than being silently absent.
    """

    @property
    def model(self) -> str:
        return "mcp/none"

    def complete(self, messages: Any, *, max_tokens: int = 1024) -> Any:
        raise ToolError(
            "This tool needs a model to interpret the question, which is not "
            "available over MCP. Use the search tool, or query through the API."
        )

    def stream(self, messages: Any, *, max_tokens: int = 1024) -> Any:
        raise ToolError("Streaming is not available over MCP.")

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def cost_of(self, usage: Any) -> float:
        return 0.0


__all__ = ["PROTOCOL_VERSION", "router"]
