"""Shaping upstream responses for a model to read.

An API returns whatever it returns — often a 400-item array with forty fields per
item. Feeding that to a model wastes context, buries the answer, and costs money.

The interesting part is **what a truncation message says**. "Response truncated"
tells a model nothing it can act on, so it re-asks the same question and gets the
same truncation. A message naming the total, the portion shown, and the
parameters available for narrowing lets it ask a better question instead. That is
the difference between a dead end and a hint.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.connectors.base import QueryResult


@dataclass(frozen=True)
class ShapingPolicy:
    """Limits applied to every response."""

    # Items kept from a JSON array. Enough to answer "what do these look like"
    # and to spot a pattern, far short of a full export.
    max_items: int = 25
    # Hard character cap after serialisation, as a backstop against a single
    # enormous object that max_items cannot bound.
    max_chars: int = 20_000


def shape_response(
    response: httpx.Response,
    *,
    request_description: str,
    policy: ShapingPolicy,
    duration_ms: float,
) -> QueryResult:
    """Turn an HTTP response into a bounded QueryResult."""
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        text = response.text[: policy.max_chars]
        return QueryResult(
            columns=("body",),
            rows=((text,),),
            sql=request_description,
            row_count=1,
            truncated=len(response.text) > policy.max_chars,
            duration_ms=duration_ms,
        )

    items, total, truncated = _select_items(payload, policy)
    columns = _columns_for(items)
    rows = tuple(
        tuple(_render(item.get(c) if isinstance(item, dict) else item) for c in columns)
        for item in items
    )

    if truncated:
        # Appended as a row so it survives every downstream rendering. A message
        # only in a log is a message the model never sees.
        rows = (*rows, tuple(_truncation_notice(columns, total, len(items), request_description)))

    return QueryResult(
        columns=columns,
        rows=rows,
        sql=request_description,
        row_count=len(items),
        truncated=truncated,
        duration_ms=duration_ms,
    )


def _select_items(payload: Any, policy: ShapingPolicy) -> tuple[list[Any], int, bool]:
    """Find the list in a response, whatever it is wrapped in."""
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        # Common envelope keys, in the order APIs tend to use them.
        for key in ("data", "items", "results", "records"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        else:
            return [payload], 1, False
    else:
        return [{"value": payload}], 1, False

    total = len(items)
    return items[: policy.max_items], total, total > policy.max_items


def _columns_for(items: list[Any]) -> tuple[str, ...]:
    """Union of keys across the items shown, in first-seen order.

    Order is stable rather than sorted so a response reads the way the API
    presented it, which is usually most-important-first.
    """
    if not items or not isinstance(items[0], dict):
        return ("value",)
    columns: list[str] = []
    for item in items:
        if isinstance(item, dict):
            columns.extend(k for k in item if k not in columns)
    return tuple(columns)


def _render(value: Any) -> str:
    """Flatten a value to a string, compactly."""
    if value is None:
        return ""
    if isinstance(value, dict | list):
        rendered = json.dumps(value, separators=(",", ":"), default=str)
        return rendered[:200] + "…" if len(rendered) > 200 else rendered
    return str(value)


def _truncation_notice(
    columns: tuple[str, ...], total: int, shown: int, request_description: str
) -> list[str]:
    """A truncation message that says what to do next.

    Naming the total and the request is what lets a model narrow its next
    attempt. "Truncated" alone produces a repeat of the same request.
    """
    message = (
        f"[truncated] Showing {shown} of {total} items from {request_description}. "
        f"To see the rest, narrow the request with a filter or a more specific "
        f"identifier rather than repeating it — the same request returns the same "
        f"{shown} items."
    )
    return [message] + [""] * (len(columns) - 1)
