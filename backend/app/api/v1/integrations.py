"""Connector configuration: the control plane.

Admin-only throughout. A connector row names a host, a database, and a
username — reconnaissance material before the credential is even considered —
and creating one points the platform's own network at an address of the
caller's choosing.

**Credentials are write-only.** They go in and are never returned: not in
plaintext, not masked, not partially, not to admins. This is stronger than the
usual "mask it in the UI", and the reason is that a maskable value is a
readable value one bug away — a serialiser change, a debug endpoint, a log line
at the wrong level. The response model has no field for it at all, so returning
one would require someone to add the field on purpose. `has_credential`, a
boolean, is what the UI needs to render "configured" versus "not set".

**Test Connection is a network probe.** An authenticated admin who can point it
anywhere can map an internal network one 200 at a time, which is SSRF with
extra steps. It is therefore admin-only, rate-limited per tenant, audited
whether it succeeds or fails, egress-checked before it opens a socket, and
returns an error *class* rather than upstream error text.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import text

from app.api.deps import CurrentPrincipal, TenantDB
from app.connectors.base import ConnectorError
from app.connectors.credentials import CredentialError, decrypt, encrypt
from app.connectors.egress import EgressBlockedError
from app.connectors.registry_store import KINDS, build_connector, validate_settings
from app.core.ratelimit import RateLimit, RateLimitExceededError, check_rate_limit
from app.core.security import Principal
from app.observability.tracing import new_trace_id, trace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["integrations"])

# Five probes a minute, per tenant. Enough to configure a connector and retry a
# typo; far too slow to enumerate a subnet. Per tenant rather than per user so
# an admin cannot multiply their allowance by creating users.
TEST_LIMIT = RateLimit(limit=5, window_seconds=60)

_SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]{1,63}$"


class ConnectorCreate(BaseModel):
    kind: str
    # Lowercase, dash/underscore only. This becomes part of a tool name the
    # model sees, so it must be predictable and free of anything that would
    # need escaping.
    slug: str = Field(pattern=_SLUG_PATTERN)
    display_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    required_labels: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    # Write-only. Never echoed back by any endpoint here.
    credential: SecretStr | None = None
    enabled: bool = True


class ConnectorUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    required_labels: list[str] | None = None
    settings: dict[str, Any] | None = None
    # Omit to keep the stored credential; send a value to replace it. There is
    # deliberately no way to *read* the current one in order to decide.
    credential: SecretStr | None = None
    enabled: bool | None = None


class ConnectorOut(BaseModel):
    """What a connector looks like to a caller.

    Note the absence: there is no credential field, and no masked stand-in for
    one. `has_credential` is what a UI needs to distinguish "configured" from
    "not set", and it discloses nothing beyond that.
    """

    id: uuid.UUID
    kind: str
    slug: str
    display_name: str
    description: str | None
    required_labels: list[str]
    enabled: bool
    settings: dict[str, Any]
    has_credential: bool
    last_tested_at: datetime | None
    last_test_ok: bool | None
    last_test_error: str | None
    created_at: datetime
    updated_at: datetime


class TestResult(BaseModel):
    ok: bool
    # An error CLASS — "connection refused", "authentication failed" — never the
    # upstream message, which carries hostnames, versions, and sometimes
    # fragments of the query.
    error: str | None = None
    duration_ms: float


def _require_admin(principal: Principal) -> None:
    if not principal.has_role("admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managing integrations requires the admin role.",
        )


def _row_to_out(row: Any) -> ConnectorOut:
    return ConnectorOut(
        id=row.id,
        kind=row.kind,
        slug=row.slug,
        display_name=row.display_name,
        description=row.description,
        required_labels=list(row.required_labels or []),
        enabled=row.enabled,
        settings=dict(row.settings or {}),
        # A boolean, derived. The value itself never leaves the database except
        # to build a connector.
        has_credential=row.credential is not None,
        last_tested_at=row.last_tested_at,
        last_test_ok=row.last_test_ok,
        last_test_error=row.last_test_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# The only columns an update may touch, and the exact SQL for each. Anything
# not named here cannot be written by this endpoint.
_ASSIGNMENTS = {
    "display_name": "display_name = :display_name",
    "description": "description = :description",
    "required_labels": "required_labels = :required_labels",
    "enabled": "enabled = :enabled",
    "settings": "settings = CAST(:settings AS jsonb)",
    "credential": "credential = :credential",
}


_SELECT = """
    SELECT id, kind, slug, display_name, description, required_labels, enabled,
           settings, credential, last_tested_at, last_test_ok, last_test_error,
           created_at, updated_at
    FROM connector
"""


@router.get("/integrations", response_model=list[ConnectorOut])
def list_integrations(principal: CurrentPrincipal, db: TenantDB) -> list[ConnectorOut]:
    """Every connector for this tenant. RLS supplies the tenant predicate."""
    _require_admin(principal)
    rows = db.execute(text(f"{_SELECT} ORDER BY created_at")).all()
    return [_row_to_out(r) for r in rows]


@router.get("/integrations/{connector_id}", response_model=ConnectorOut)
def get_integration(
    connector_id: uuid.UUID, principal: CurrentPrincipal, db: TenantDB
) -> ConnectorOut:
    _require_admin(principal)
    row = db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": connector_id}).first()
    if row is None:
        # 404 whether it does not exist or belongs to another tenant. RLS makes
        # the latter indistinguishable already; saying so explicitly keeps it
        # that way if the query ever changes.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such integration.")
    return _row_to_out(row)


@router.post("/integrations", response_model=ConnectorOut, status_code=status.HTTP_201_CREATED)
def create_integration(
    body: ConnectorCreate, principal: CurrentPrincipal, db: TenantDB
) -> ConnectorOut:
    """Create a connector.

    Settings are validated against the model for their kind before anything is
    written, so a malformed connector fails here rather than at first query.
    """
    _require_admin(principal)

    if body.kind not in KINDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"kind must be one of {list(KINDS)}"
        )
    try:
        settings = validate_settings(body.kind, body.settings)
    except ValueError as exc:
        # A configuration mistake SHOULD be legible to whoever made it. This is
        # the opposite of the authorization errors elsewhere, which must not be.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    ciphertext = _encrypt_or_422(body.credential)

    exists = db.execute(
        text("SELECT 1 FROM connector WHERE slug = :slug"), {"slug": body.slug}
    ).first()
    if exists:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"An integration named {body.slug!r} already exists."
        )

    new_id = uuid.uuid4()
    db.execute(
        text("""
            INSERT INTO connector
              (id, tenant_id, kind, slug, display_name, description, required_labels,
               enabled, settings, credential)
            VALUES
              (:id, :tenant, :kind, :slug, :name, :description, :labels,
               :enabled, CAST(:settings AS jsonb), :credential)
        """),
        {
            "id": new_id,
            # Server-derived, never from the body. Invariant #1.
            "tenant": principal.tenant_id,
            "kind": body.kind,
            "slug": body.slug,
            "name": body.display_name,
            "description": body.description,
            "labels": body.required_labels,
            "enabled": body.enabled,
            "settings": _json(settings),
            "credential": ciphertext,
        },
    )

    logger.info(
        "connector created: tenant=%s slug=%s kind=%s by=%s",
        principal.tenant_id,
        body.slug,
        body.kind,
        principal.email,
    )

    row = db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": new_id}).one()
    return _row_to_out(row)


@router.patch("/integrations/{connector_id}", response_model=ConnectorOut)
def update_integration(
    connector_id: uuid.UUID,
    body: ConnectorUpdate,
    principal: CurrentPrincipal,
    db: TenantDB,
) -> ConnectorOut:
    """Update a connector.

    Omitted fields are left alone. Omitting `credential` keeps the stored one,
    which is the only way to edit a connector without knowing its password —
    and since the password cannot be read back, it is the usual way.
    """
    _require_admin(principal)

    row = db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": connector_id}).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such integration.")

    updates: dict[str, Any] = {}
    if body.display_name is not None:
        updates["display_name"] = body.display_name
    if body.description is not None:
        updates["description"] = body.description
    if body.required_labels is not None:
        updates["required_labels"] = body.required_labels
    if body.enabled is not None:
        updates["enabled"] = body.enabled
    if body.settings is not None:
        try:
            updates["settings"] = _json(validate_settings(row.kind, body.settings))
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if body.credential is not None:
        updates["credential"] = _encrypt_or_422(body.credential)

    if not updates:
        return _row_to_out(row)

    # Built from a fixed allowlist rather than from the keys of `updates`.
    # Those keys are set by this function and are safe today; the allowlist is
    # what keeps that true if a later edit ever routes a caller-supplied name
    # into the dict. An UPDATE assembled by f-string is a pattern worth making
    # structurally safe rather than defending with a comment.
    assignments = ", ".join(_ASSIGNMENTS[col] for col in updates)
    # The suppression is justified by _ASSIGNMENTS: every fragment joined into
    # `assignments` is a literal defined in this module, and every VALUE is
    # still bound as a parameter. Ruff cannot see that, and the rule is a good
    # one to leave switched on everywhere else.
    statement = f"UPDATE connector SET {assignments}, updated_at = now() WHERE id = :id"  # noqa: S608
    db.execute(text(statement), {**updates, "id": connector_id})

    logger.info(
        "connector updated: tenant=%s slug=%s fields=%s by=%s",
        principal.tenant_id,
        row.slug,
        sorted(updates),
        principal.email,
    )

    updated = db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": connector_id}).one()
    return _row_to_out(updated)


@router.delete("/integrations/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration(connector_id: uuid.UUID, principal: CurrentPrincipal, db: TenantDB) -> None:
    _require_admin(principal)
    row = db.execute(
        text("SELECT slug FROM connector WHERE id = :id"), {"id": connector_id}
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such integration.")

    db.execute(text("DELETE FROM connector WHERE id = :id"), {"id": connector_id})
    logger.info(
        "connector deleted: tenant=%s slug=%s by=%s",
        principal.tenant_id,
        row.slug,
        principal.email,
    )


@router.post("/integrations/{connector_id}/test", response_model=TestResult)
def test_integration(
    connector_id: uuid.UUID, principal: CurrentPrincipal, db: TenantDB
) -> TestResult:
    """Probe a connector, and record what happened.

    Every guard in the module docstring applies here. The order matters: the
    rate limit is consumed before the connector is built, so a caller cannot
    spend the platform's time on egress resolution and TCP timeouts for free.
    """
    _require_admin(principal)

    try:
        check_rate_limit(f"conn-test:{principal.tenant_id}", TEST_LIMIT)
    except RateLimitExceededError as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many connection tests. Try again shortly.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc

    row = db.execute(text(f"{_SELECT} WHERE id = :id"), {"id": connector_id}).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such integration.")

    trace_id = new_trace_id()
    started = datetime.now(UTC)

    with trace(db, "connector.test", tenant_id=principal.tenant_id, trace_id=trace_id) as span:
        ok, error = _probe(row)
        span.set_attribute("connector", row.slug)
        span.set_attribute("ok", ok)
        if error:
            span.set_attribute("error_class", error)

    duration_ms = (datetime.now(UTC) - started).total_seconds() * 1000

    db.execute(
        text("""
            UPDATE connector
            SET last_tested_at = now(), last_test_ok = :ok, last_test_error = :err
            WHERE id = :id
        """),
        {"ok": ok, "err": error, "id": connector_id},
    )

    # Audited whether it succeeded or not. A run of failures against varied
    # hosts is what network mapping looks like, and it is invisible if only
    # successes are recorded.
    logger.info(
        "connector test: tenant=%s slug=%s ok=%s by=%s",
        principal.tenant_id,
        row.slug,
        ok,
        principal.email,
    )

    return TestResult(ok=ok, error=error, duration_ms=round(duration_ms, 2))


def _probe(row: Any) -> tuple[bool, str | None]:
    """Build the connector and ask it whether it is reachable.

    Returns an error CLASS, never upstream text. A message echoing "could not
    connect to server at 10.0.3.14:5432" turns a failed probe into a successful
    scan.
    """
    try:
        credential = decrypt(row.credential) if row.credential else None
    except CredentialError:
        return False, "stored credential could not be decrypted"

    try:
        connector = build_connector(
            kind=row.kind,
            slug=row.slug,
            display_name=row.display_name,
            settings=dict(row.settings or {}),
            required_labels=tuple(row.required_labels or []),
            credential=credential,
        )
    except EgressBlockedError:
        return False, "address not permitted by egress policy"
    except ValueError as exc:
        return False, str(exc)[:200]
    except ConnectorError:
        return False, "connector could not be constructed"
    except OSError:
        return False, "host unreachable"

    try:
        healthy = connector.health()
    except ConnectorError:
        return False, "connection failed"
    except OSError:
        return False, "host unreachable"
    except Exception:  # noqa: BLE001 — never surface an upstream driver message
        logger.exception("connector test raised an unexpected error")
        return False, "connection failed"

    return (True, None) if healthy else (False, "connection failed")


def _encrypt_or_422(credential: SecretStr | None) -> str | None:
    if credential is None:
        return None
    try:
        return encrypt(credential)
    except CredentialError as exc:
        # The message names the missing configuration, never the value.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value)
