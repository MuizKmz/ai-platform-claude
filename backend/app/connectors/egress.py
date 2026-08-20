"""Egress control: where a connector is allowed to connect.

A connector's host comes from configuration, and configuration can be wrong,
stale, or hostile. This module is what stands between "someone configured a
connector" and "the platform made a connection on their behalf".

The attack this exists to stop is SSRF against cloud metadata. `169.254.169.254`
answers on AWS, GCP, and Azure with instance credentials, and it needs no
authentication — anything that can make an HTTP or TCP connection from inside the
VPC can read them. A connector pointed there is a credential exfiltration tool
wearing a database client's clothes.

**DNS is re-resolved at connect time.** Validating a hostname once and connecting
later is the DNS rebinding hole: an attacker returns a public address for the
check and a private one milliseconds later. The only sound approach is to resolve,
validate every address the name returns, and connect to a validated address.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass


class EgressBlockedError(Exception):
    """The destination is not permitted. Message is safe to log and to return —
    it names a host the caller already supplied."""


# Networks that are never a legitimate connector target.
#
# Link-local carries cloud metadata. Loopback would let a connector reach the
# platform's own services, including Postgres as a role it was never granted.
# The rest are documented as unroutable and appearing only by mistake or design.
_ALWAYS_BLOCKED = (
    ipaddress.ip_network("169.254.0.0/16"),  # link-local, incl. cloud metadata
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("0.0.0.0/8"),  # "this network"
    ipaddress.ip_network("100.64.0.0/10"),  # carrier-grade NAT
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("240.0.0.0/4"),  # reserved
)

# Private ranges. Blocked by default but legitimately needed for a database on the
# same VPC, so they are permitted only by explicit opt-in.
_PRIVATE = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique local
)


@dataclass(frozen=True)
class EgressPolicy:
    """What a connector may connect to."""

    # Opt-in for a database inside the same private network. Never enables
    # link-local, which stays blocked regardless.
    allow_private: bool = False
    # Opt-in for loopback, for development where the target database runs on
    # the same host. Separate from allow_private and default-off because
    # loopback reaches the platform's OWN services — including the Postgres
    # whose Row-Level Security everything else depends on. Enabling this in a
    # real deployment would let a connector read as a role it was never
    # granted.
    allow_loopback: bool = False
    # If non-empty, ONLY these hostnames may be used. An allowlist is the strong
    # form; the IP checks below remain as a backstop against a permitted name
    # resolving somewhere it should not.
    allowed_hosts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ResolvedTarget:
    """A destination that has passed validation.

    Carries the validated IP so the caller connects to *that*, not to the
    hostname again — re-resolving after the check reopens the rebinding window
    the check was there to close.
    """

    host: str
    ip: str
    port: int


def resolve_and_validate(host: str, port: int, policy: EgressPolicy) -> ResolvedTarget:
    """Resolve a host and confirm every address it returns is permitted.

    EVERY address, not the first: a name returning one public and one link-local
    address must be refused, because which one is used is not under our control.
    """
    if policy.allowed_hosts and host not in policy.allowed_hosts:
        raise EgressBlockedError(f"Host {host!r} is not on the connector egress allowlist.")

    if not 1 <= port <= 65535:
        raise EgressBlockedError(f"Port {port} is out of range.")

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise EgressBlockedError(f"Host {host!r} could not be resolved.") from exc

    # sockaddr[0] is the address string for both AF_INET and AF_INET6; the
    # annotation is a union because sockaddr shapes differ between families.
    addresses = {str(info[4][0]) for info in infos}
    if not addresses:
        raise EgressBlockedError(f"Host {host!r} resolved to no addresses.")

    for address in addresses:
        _assert_permitted(address, host, policy)

    # Prefer IPv4 when both are offered, for predictable connection behaviour.
    chosen = sorted(addresses, key=lambda a: ":" in a)[0]
    return ResolvedTarget(host=host, ip=chosen, port=port)


def _assert_permitted(address: str, host: str, policy: EgressPolicy) -> None:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as exc:
        raise EgressBlockedError(f"Host {host!r} resolved to an unparseable address.") from exc

    if policy.allow_loopback and ip.is_loopback:
        return

    for network in _ALWAYS_BLOCKED:
        if ip in network:
            # The message names the range, not the address: on a metadata probe
            # the interesting information is that it was blocked, and echoing the
            # resolved address back confirms the resolution succeeded.
            raise EgressBlockedError(
                f"Host {host!r} resolves into a blocked range ({network}). "
                "Link-local and loopback destinations are never permitted."
            )

    if not policy.allow_private:
        for network in _PRIVATE:
            if ip in network:
                raise EgressBlockedError(
                    f"Host {host!r} resolves into a private range ({network}). "
                    "Set allow_private on the connector's egress policy if this is intended."
                )
