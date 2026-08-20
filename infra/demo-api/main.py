"""A stand-in business API, for proving Phase 9's write path.

Not part of the platform. This is the *target* — the thing a write connector
writes to — standing in for whatever real system a deployment would point at:
a ticketing API, an ERP's REST surface, a WMS endpoint.

**Why a real service rather than a mock.** The two properties Phase 9 has to
demonstrate are idempotency and compensating actions, and both are properties of
an HTTP conversation. A mock that returns whatever the test wants proves the
platform calls something; it does not prove that calling twice creates one
ticket, because the mock is the thing deciding that. This service implements the
behaviour a real API would, so the tests observe it rather than assert it.

**Deliberately small and deliberately strict.** It rejects a write with no
`Idempotency-Key`, because a target that tolerates missing keys would let the
platform ship without sending them and nobody would notice until a duplicate
appeared in production.

Run it:

    uv run --with fastapi --with uvicorn uvicorn main:app --port 9100

State is in memory. Restarting forgets every ticket, which is correct for a
fixture and would be catastrophic for a real system.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

app = FastAPI(
    title="Demo Business API",
    description="A stand-in write target for the platform's approval flow.",
    version="1.0.0",
)

# ticket_id -> ticket
_TICKETS: dict[str, dict[str, Any]] = {}
# idempotency key -> ticket_id. The whole point of the service.
_KEYS: dict[str, str] = {}


class TicketCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=4000)
    priority: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")
    assignee: str | None = Field(default=None, max_length=200)


class Ticket(BaseModel):
    id: str
    title: str
    body: str
    priority: str
    assignee: str | None
    status: str
    created_at: datetime
    cancelled_at: datetime | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "tickets": str(len(_TICKETS))}


@app.post("/tickets", response_model=Ticket, status_code=status.HTTP_201_CREATED)
def create_ticket(
    ticket: TicketCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Ticket:
    """Create a ticket, at most once per idempotency key.

    Returns **201** for a new ticket and **200** for a replay. The distinction
    is the observable difference between "this created something" and "this
    found what your last call created", and a client that treats them the same
    cannot tell a duplicate from a success.
    """
    if not idempotency_key:
        # Refused rather than tolerated. A target that accepts writes without a
        # key would let the platform omit them, and the omission would only
        # surface as a duplicate in production.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Idempotency-Key header is required for writes.",
        )

    existing_id = _KEYS.get(idempotency_key)
    if existing_id:
        response.status_code = status.HTTP_200_OK
        return Ticket(**_TICKETS[existing_id])

    ticket_id = f"TKT-{uuid.uuid4().hex[:8].upper()}"
    record = {
        "id": ticket_id,
        "title": ticket.title,
        "body": ticket.body,
        "priority": ticket.priority,
        "assignee": ticket.assignee,
        "status": "open",
        "created_at": datetime.now(UTC),
        "cancelled_at": None,
    }
    _TICKETS[ticket_id] = record
    _KEYS[idempotency_key] = ticket_id
    return Ticket(**record)


@app.get("/tickets/{ticket_id}", response_model=Ticket)
def get_ticket(ticket_id: str) -> Ticket:
    if ticket_id not in _TICKETS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such ticket.")
    return Ticket(**_TICKETS[ticket_id])


@app.get("/tickets", response_model=list[Ticket])
def list_tickets() -> list[Ticket]:
    return [Ticket(**t) for t in _TICKETS.values()]


@app.delete("/tickets/{ticket_id}", response_model=Ticket)
def cancel_ticket(ticket_id: str) -> Ticket:
    """The compensating action.

    Cancels rather than deletes. "This was raised and then withdrawn" and "this
    never happened" are different facts, and a real ticketing system would
    record the first — so the platform's compensation story is tested against
    the behaviour it will actually meet.
    """
    if ticket_id not in _TICKETS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such ticket.")

    record = _TICKETS[ticket_id]
    if record["status"] != "cancelled":
        record["status"] = "cancelled"
        record["cancelled_at"] = datetime.now(UTC)
    return Ticket(**record)


@app.post("/_reset", include_in_schema=False)
def reset() -> dict[str, str]:
    """Clear all state. For tests only — a real API would never expose this."""
    _TICKETS.clear()
    _KEYS.clear()
    return {"status": "reset"}
