"""Egress control and credential storage.

The egress tests exist for one attack in particular: a connector pointed at
169.254.169.254 returns cloud instance credentials to anyone who can configure a
connector. No authentication is involved on the metadata side — the only defence
is refusing to make the connection.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from app.connectors.credentials import CredentialError, decrypt, encrypt
from app.connectors.egress import (
    EgressBlockedError,
    EgressPolicy,
    resolve_and_validate,
)

pytestmark = pytest.mark.security


def _resolving_to(*addresses: str) -> list:
    """Fake getaddrinfo output for the given addresses."""
    return [
        (
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (address, 5432),
        )
        for address in addresses
    ]


# --- the attack this module exists to stop ------------------------------------


def test_egress_blocks_link_local() -> None:
    """169.254.169.254 is the cloud metadata endpoint on AWS, GCP and Azure.

    It serves instance credentials to anything that can reach it, with no
    authentication.
    """
    with (
        patch("socket.getaddrinfo", return_value=_resolving_to("169.254.169.254")),
        pytest.raises(EgressBlockedError, match="blocked range"),
    ):
        resolve_and_validate("metadata.internal", 80, EgressPolicy(allow_private=True))


def test_egress_blocks_dns_rebinding() -> None:
    """A hostname resolving to a private address at connect time is refused.

    Validating a name once and connecting later is the rebinding hole. This
    checks what the name resolves to NOW, which is the only sound moment.
    """
    with (
        patch("socket.getaddrinfo", return_value=_resolving_to("10.0.0.5")),
        pytest.raises(EgressBlockedError, match="private range"),
    ):
        resolve_and_validate("innocent.example.com", 5432, EgressPolicy())


def test_a_name_resolving_to_several_addresses_must_pass_them_all() -> None:
    """One public and one link-local address is a refusal.

    Which address the OS would have used is not under our control, so a single
    bad answer poisons the name.
    """
    with (
        patch("socket.getaddrinfo", return_value=_resolving_to("93.184.216.34", "169.254.169.254")),
        pytest.raises(EgressBlockedError),
    ):
        resolve_and_validate("split.example.com", 443, EgressPolicy())


def test_loopback_is_blocked_even_when_private_is_allowed() -> None:
    """Loopback would let a connector reach the platform's own Postgres as a role
    it was never granted. allow_private must not open that."""
    with (
        patch("socket.getaddrinfo", return_value=_resolving_to("127.0.0.1")),
        pytest.raises(EgressBlockedError, match="blocked range"),
    ):
        resolve_and_validate("localhost", 5432, EgressPolicy(allow_private=True))


@pytest.mark.parametrize(
    "address",
    # S104 flags 0.0.0.0 as a bind-to-all-interfaces risk. Here it is a
    # destination being asserted BLOCKED, which is the opposite concern.
    ["0.0.0.0", "100.64.0.1", "224.0.0.1", "240.0.0.1", "::1", "fe80::1"],  # noqa: S104
)
def test_reserved_ranges_are_blocked(address: str) -> None:
    with (
        patch("socket.getaddrinfo", return_value=_resolving_to(address)),
        pytest.raises(EgressBlockedError),
    ):
        resolve_and_validate("host.example.com", 5432, EgressPolicy(allow_private=True))


# --- private ranges: blocked by default, opt-in permitted ---------------------


def test_private_addresses_are_blocked_by_default() -> None:
    for address in ("10.1.2.3", "172.16.0.1", "192.168.1.1"):
        with (
            patch("socket.getaddrinfo", return_value=_resolving_to(address)),
            pytest.raises(EgressBlockedError, match="private range"),
        ):
            resolve_and_validate("db.internal", 5432, EgressPolicy())


def test_private_addresses_are_allowed_when_opted_in() -> None:
    """A database on the same VPC is a legitimate target — deliberately declared."""
    with patch("socket.getaddrinfo", return_value=_resolving_to("10.1.2.3")):
        target = resolve_and_validate("db.internal", 5432, EgressPolicy(allow_private=True))

    assert target.ip == "10.1.2.3"
    assert target.port == 5432


def test_public_addresses_are_permitted() -> None:
    with patch("socket.getaddrinfo", return_value=_resolving_to("93.184.216.34")):
        target = resolve_and_validate("example.com", 5432, EgressPolicy())

    assert target.ip == "93.184.216.34"


# --- allowlist ----------------------------------------------------------------


def test_allowlist_refuses_unlisted_hosts_before_resolving() -> None:
    """The name is refused without a DNS lookup, so an unlisted host cannot even
    be used to probe what resolves."""
    with (
        patch("socket.getaddrinfo") as resolver,
        pytest.raises(EgressBlockedError, match="allowlist"),
    ):
        resolve_and_validate(
            "evil.example.com", 5432, EgressPolicy(allowed_hosts=frozenset({"db.example.com"}))
        )

    resolver.assert_not_called()


def test_allowlisted_host_still_faces_the_ip_checks() -> None:
    """An allowlist entry that resolves somewhere forbidden is still refused —
    the two layers are independent."""
    policy = EgressPolicy(allowed_hosts=frozenset({"db.example.com"}))

    with (
        patch("socket.getaddrinfo", return_value=_resolving_to("169.254.169.254")),
        pytest.raises(EgressBlockedError, match="blocked range"),
    ):
        resolve_and_validate("db.example.com", 5432, policy)


# --- input validation ---------------------------------------------------------


def test_unresolvable_host_is_refused() -> None:
    with (
        patch("socket.getaddrinfo", side_effect=socket.gaierror("nope")),
        pytest.raises(EgressBlockedError, match="could not be resolved"),
    ):
        resolve_and_validate("nowhere.invalid", 5432, EgressPolicy())


@pytest.mark.parametrize("port", [0, -1, 70000])
def test_out_of_range_ports_are_refused(port: int) -> None:
    with pytest.raises(EgressBlockedError, match="out of range"):
        resolve_and_validate("example.com", port, EgressPolicy())


# --- credentials --------------------------------------------------------------


def test_credentials_round_trip() -> None:
    secret = SecretStr("hunter2-database-password")

    ciphertext = encrypt(secret)
    recovered = decrypt(ciphertext)

    assert recovered.get_secret_value() == secret.get_secret_value()


def test_ciphertext_does_not_contain_the_plaintext() -> None:
    """The stored value is what ends up in a backup or a stolen dump."""
    ciphertext = encrypt(SecretStr("hunter2-database-password"))

    assert "hunter2" not in ciphertext


def test_credentials_never_in_logs_or_traces() -> None:
    """SecretStr renders as asterisks in repr, str, and f-strings.

    That covers the ordinary accidents: a log line, an exception message, a
    Pydantic response model. The value comes out only via .get_secret_value(),
    which is greppable in review.
    """
    secret = SecretStr("hunter2-database-password")

    for rendering in (repr(secret), str(secret), f"{secret}"):
        assert "hunter2" not in rendering
        assert "*" in rendering


def test_tampered_ciphertext_is_refused() -> None:
    """Fernet authenticates, so a modified token fails rather than decrypting to
    something plausible."""
    ciphertext = encrypt(SecretStr("original-password"))
    tampered = ciphertext[:-6] + "AAAAAA"

    with pytest.raises(CredentialError, match="could not be decrypted"):
        decrypt(tampered)


def test_decryption_failure_says_nothing_useful() -> None:
    """An attacker probing with guessed ciphertext learns only that it failed."""
    with pytest.raises(CredentialError) as exc:
        decrypt("not-a-valid-token")

    message = str(exc.value).lower()
    assert "key" not in message
    assert "fernet" not in message


def test_encryption_requires_a_configured_key() -> None:
    """A default key would be shared by every deployment that forgot to set one."""
    with (
        patch("app.connectors.credentials.settings") as fake_settings,
        pytest.raises(CredentialError, match="CREDENTIAL_ENCRYPTION_KEY"),
    ):
        fake_settings.credential_encryption_key = ""
        encrypt(SecretStr("anything"))
