"""The realm file must produce a token the API can actually use.

Every one of these assertions exists because the realm file failed it against a
real Keycloak 26 during Phase 11. None were predicted by reading the docs:

- `_comment` keys fail the whole import — Keycloak rejects unknown fields, and
  the container restart-loops rather than starting without them.
- `waitIncrementsSeconds` is not a field. It is `waitIncrementSeconds`.
- `userProfile` is not an inline realm key. It is a component whose config is
  the profile serialised as a *string*.
- Declaring `clientScopes` REPLACES Keycloak's built-in set, so `email`,
  `roles`, and — most damagingly — `sub` silently vanish from every token.

That last one is the reason this file exists. A token missing `sub` verifies
perfectly and then fails `principal_from_token` with "missing required identity
claims", which reads like a bad password and is not.

These are static checks against the JSON. They do not need Keycloak running,
which is the point: a broken realm should fail in CI in milliseconds rather than
90 seconds into a container boot on someone's laptop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.core.config import settings

pytestmark = pytest.mark.security

REALM_FILE = Path(__file__).resolve().parents[3] / "infra" / "keycloak" / "realm-eaip.json"


@pytest.fixture(scope="module")
def realm() -> dict[str, Any]:
    with REALM_FILE.open(encoding="utf-8") as handle:
        loaded: dict[str, Any] = json.load(handle)
    return loaded


@pytest.fixture(scope="module")
def identity_scope(realm: dict[str, Any]) -> dict[str, Any]:
    for scope in realm["clientScopes"]:
        if scope["name"] == "eaip-identity":
            return scope
    pytest.fail("the eaip-identity client scope is missing")


def _mappers(scope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {mapper["name"]: mapper for mapper in scope["protocolMappers"]}


def test_the_realm_file_is_valid_json(realm: dict[str, Any]) -> None:
    assert realm["realm"] == "eaip"


def test_no_comment_keys_anywhere(realm: dict[str, Any]) -> None:
    """Keycloak rejects unknown fields, and a stray comment fails the import.

    Documented in infra/keycloak/README.md, and enforced here because the
    failure mode is a restart-looping container rather than an error anyone
    reads.
    """

    def walk(node: Any, path: str = "") -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key.startswith("_"):
                    found.append(f"{path}.{key}")
                found.extend(walk(value, f"{path}.{key}"))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                found.extend(walk(item, f"{path}[{index}]"))
        return found

    assert not walk(realm), "underscore-prefixed keys fail the realm import"


# --- the claims the API requires ---------------------------------------------


def test_every_claim_the_api_requires_has_a_mapper(identity_scope: dict[str, Any]) -> None:
    """`sub` is the one that bit us.

    It normally arrives via the built-in `basic` scope, which declaring
    clientScopes removes. app/core/oidc.py requires it and derives user_id from
    it, so without a mapper every login fails on an otherwise perfect token.
    """
    mappers = _mappers(identity_scope)
    assert "eaip-subject" in mappers, "no `sub` mapper: every login would fail"
    assert "eaip-email" in mappers, "no email mapper: Principal.email cannot be derived"
    assert "eaip-tenant-id" in mappers, "no tenant mapper: invariant #1 cannot be satisfied"
    assert "eaip-labels" in mappers, "no labels mapper: retrieval would filter on nothing"
    assert "eaip-realm-roles" in mappers, "no roles mapper: every authorization check fails closed"


def test_the_tenant_claim_matches_what_the_api_reads(identity_scope: dict[str, Any]) -> None:
    """The realm and the application must agree on the claim name.

    They are configured independently — one in JSON, one in settings — so a
    rename on either side is exactly the kind of change that passes review.
    """
    mapper = _mappers(identity_scope)["eaip-tenant-id"]
    # Keycloak treats the claim name as a dotted path, so the file escapes the
    # dot to keep the claim flat rather than nested under an `eaip` object.
    claim = mapper["config"]["claim.name"].replace("\\.", ".")
    assert claim == settings.oidc_tenant_claim


def test_the_labels_claim_matches_what_the_api_reads(identity_scope: dict[str, Any]) -> None:
    mapper = _mappers(identity_scope)["eaip-labels"]
    claim = mapper["config"]["claim.name"].replace("\\.", ".")
    assert claim == settings.oidc_labels_claim


def test_the_roles_claim_matches_what_the_api_reads(identity_scope: dict[str, Any]) -> None:
    """The API reads `realm_access`; the mapper writes `realm_access.roles`."""
    mapper = _mappers(identity_scope)["eaip-realm-roles"]
    assert mapper["config"]["claim.name"].split(".")[0] == settings.oidc_roles_claim


def test_claims_reach_the_access_token_not_only_the_id_token(
    identity_scope: dict[str, Any],
) -> None:
    """The API reads the access token. A claim only in the ID token is invisible."""
    for name in ("eaip-tenant-id", "eaip-labels", "eaip-email", "eaip-realm-roles"):
        config = _mappers(identity_scope)[name]["config"]
        assert config.get("access.token.claim") == "true", f"{name} never reaches the access token"


# --- audience separation (RFC 8707, ADR 0007) ---------------------------------


def test_the_api_audience_is_issued(identity_scope: dict[str, Any]) -> None:
    """Without this the aud is the requesting client, and the API's check fails."""
    mapper = _mappers(identity_scope)["eaip-api-audience"]
    assert mapper["config"]["included.custom.audience"] == settings.jwt_audience


def test_console_and_mcp_are_separate_clients(realm: dict[str, Any]) -> None:
    """A console token must not work as a machine credential."""
    clients = {client["clientId"]: client for client in realm["clients"]}
    assert "eaip-console" in clients
    assert "eaip-mcp" in clients
    assert clients["eaip-console"]["publicClient"] is True
    assert clients["eaip-mcp"]["publicClient"] is False


# --- posture ------------------------------------------------------------------


def test_the_console_client_keeps_no_secret_and_uses_pkce(realm: dict[str, Any]) -> None:
    """A browser app cannot hold a secret; PKCE is what secures the flow."""
    console = next(c for c in realm["clients"] if c["clientId"] == "eaip-console")
    assert console["publicClient"] is True
    assert console["attributes"]["pkce.code.challenge.method"] == "S256"
    # Implicit returns tokens in the URL fragment, where they reach history and
    # logs. Superseded by PKCE and off deliberately.
    assert console["implicitFlowEnabled"] is False
    # Direct access grants would let the console collect passwords itself,
    # which is the thing an identity provider exists to stop.
    assert console["directAccessGrantsEnabled"] is False


def test_registration_is_closed(realm: dict[str, Any]) -> None:
    """This is an internal platform. Self-signup would create untenanted users."""
    assert realm["registrationAllowed"] is False


def test_brute_force_protection_is_on(realm: dict[str, Any]) -> None:
    """The CLI issuer had no such concept because it checked no credentials."""
    assert realm["bruteForceProtected"] is True
    assert realm["failureFactor"] <= 10


def test_tokens_are_short_lived(realm: dict[str, Any]) -> None:
    """Short tokens are what make revocation meaningful.

    Disabling a user ends their access within this window rather than whenever
    their token happens to expire. The CLI tokens lasted an hour and could not
    be revoked at all.
    """
    assert realm["accessTokenLifespan"] <= 900


def test_no_users_are_defined(realm: dict[str, Any]) -> None:
    """A realm that ships a working account ships a default credential."""
    assert not realm.get("users")


def test_the_user_profile_declares_the_eaip_attributes(realm: dict[str, Any]) -> None:
    """Keycloak 26 silently DISCARDS attributes it has not been told about.

    Not an error — the user is created, the attribute is dropped, and the
    mapper emits nothing. Every login then fails on a missing tenant claim,
    with nothing in the logs pointing at the cause.
    """
    components = realm["components"]["org.keycloak.userprofile.UserProfileProvider"]
    config = components[0]["config"]["kc.user.profile.config"][0]
    declared = {attribute["name"] for attribute in json.loads(config)["attributes"]}
    assert "tenant_id" in declared, "tenant_id would be silently dropped on every user"
    assert "labels" in declared, "labels would be silently dropped on every user"
