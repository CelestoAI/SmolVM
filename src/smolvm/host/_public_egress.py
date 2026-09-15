# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Internal public-egress policy contract.

This module deliberately does not expose a user-selectable network mode.  It is
the policy seam that a later host proxy will call immediately before opening
each upstream connection.  A decision approves exactly one pinned address;
redirects and subsequent connections must be resolved and checked again.
"""

from __future__ import annotations

import socket
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
from typing import Protocol, TypeAlias

IPAddress: TypeAlias = IPv4Address | IPv6Address
IPNetwork: TypeAlias = IPv4Network | IPv6Network

# Fixed guest endpoint reserved for QEMU slirp ``guestfwd``. Keeping it with
# the policy contract lets both the QEMU adapter and browser launcher share the
# value without importing either runtime into the other.
QEMU_PUBLIC_PROXY_GUEST_IP = "10.0.2.100"
QEMU_PUBLIC_PROXY_GUEST_PORT = 3128


class EgressTransport(str, Enum):
    """Transport presented to the future host proxy."""

    HTTP = "http"
    HTTPS = "https"
    WEBSOCKET = "websocket"
    WEBSOCKET_SECURE = "websocket_secure"
    DIRECT_DNS = "direct_dns"
    RAW_SOCKET = "raw_socket"


class EgressRequestKind(str, Enum):
    """Browser activity that caused an upstream connection."""

    NAVIGATION = "navigation"
    REDIRECT = "redirect"
    SUBRESOURCE = "subresource"
    SERVICE_WORKER = "service_worker"
    WEBSOCKET = "websocket"


class EgressBlockReason(str, Enum):
    """Stable, non-sensitive reasons for denying a connection."""

    INVALID_TARGET = "invalid_target"
    UNSUPPORTED_TRANSPORT = "unsupported_transport"
    DNS_RESOLUTION_FAILED = "dns_resolution_failed"
    NO_ADDRESSES = "no_addresses"
    MIXED_ADDRESS_SCOPE = "mixed_address_scope"
    IPV4_MAPPED = "ipv4_mapped"
    NETWORK_METADATA = "network_metadata"
    UNSPECIFIED = "unspecified"
    LOOPBACK = "loopback"
    LINK_LOCAL = "link_local"
    PRIVATE = "private"
    SHARED = "shared"
    MULTICAST = "multicast"
    RESERVED = "reserved"
    NON_GLOBAL = "non_global"


@dataclass(frozen=True)
class EgressRequest:
    """One connection attempt evaluated at the host proxy boundary."""

    hostname: str
    port: int
    transport: EgressTransport
    kind: EgressRequestKind


@dataclass(frozen=True)
class AddressClassification:
    """Public-egress classification for one resolved address."""

    address: IPAddress
    allowed: bool
    block_reason: EgressBlockReason | None = None


@dataclass(frozen=True)
class ResolvedPublicEndpoint:
    """A checked endpoint approved for one connection.

    Connectors must use ``pinned_address`` rather than resolving ``hostname``
    again.  ``resolved_addresses`` is diagnostic context, not a fallback list.
    """

    hostname: str
    port: int
    resolved_addresses: tuple[IPAddress, ...]
    pinned_address: IPAddress


@dataclass(frozen=True)
class EgressDecision:
    """Result of applying the public-egress policy to a connection."""

    allowed: bool
    endpoint: ResolvedPublicEndpoint | None = None
    block_reason: EgressBlockReason | None = None
    classifications: tuple[AddressClassification, ...] = ()


class AddressResolver(Protocol):
    """Connection-time hostname resolver used by the policy boundary."""

    def resolve(self, hostname: str, port: int) -> Sequence[str]: ...


class SocketAddressResolver:
    """Resolve TCP addresses with the host operating system."""

    def resolve(self, hostname: str, port: int) -> Sequence[str]:
        answers = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        return tuple(str(answer[4][0]) for answer in answers)


_METADATA_NETWORKS: tuple[IPNetwork, ...] = (
    ip_network("169.254.169.254/32"),
    ip_network("169.254.170.2/32"),
    ip_network("100.100.100.200/32"),
    ip_network("fd00:ec2::254/128"),
    ip_network("fe80::a9fe:a9fe/128"),
)
_SHARED_ADDRESS_SPACE = ip_network("100.64.0.0/10")
# Keep classification stable on every supported Python version.  The stdlib's
# ``is_global`` tables follow IANA but have changed between Python 3.11 and
# 3.13, notably for these newer special-purpose allocations.
_NON_GLOBAL_NETWORKS: tuple[IPNetwork, ...] = (
    ip_network("192.0.0.0/24"),
    ip_network("192.0.2.0/24"),
    ip_network("192.88.99.0/24"),
    ip_network("198.18.0.0/15"),
    ip_network("198.51.100.0/24"),
    ip_network("203.0.113.0/24"),
    ip_network("64:ff9b:1::/48"),
    ip_network("100::/64"),
    ip_network("2001:db8::/32"),
    ip_network("2002::/16"),
)
_GLOBAL_EXCEPTIONS: tuple[IPNetwork, ...] = (
    ip_network("192.0.0.9/32"),
    ip_network("192.0.0.10/32"),
)
_PROXY_TRANSPORTS = {
    EgressTransport.HTTP,
    EgressTransport.HTTPS,
    EgressTransport.WEBSOCKET,
    EgressTransport.WEBSOCKET_SECURE,
}


def classify_public_address(value: str | IPAddress) -> AddressClassification:
    """Classify an IP address for public-only egress.

    Only globally routable, non-mapped addresses pass.  Explicit checks retain
    useful block reasons instead of collapsing every denial into ``non_global``.
    """

    address = ip_address(value) if isinstance(value, str) else value
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return _blocked(address, EgressBlockReason.IPV4_MAPPED)
    if any(address in network for network in _METADATA_NETWORKS):
        return _blocked(address, EgressBlockReason.NETWORK_METADATA)
    if address.is_unspecified:
        return _blocked(address, EgressBlockReason.UNSPECIFIED)
    if address.is_loopback:
        return _blocked(address, EgressBlockReason.LOOPBACK)
    if address.is_link_local:
        return _blocked(address, EgressBlockReason.LINK_LOCAL)
    if isinstance(address, IPv6Address) and address.is_site_local:
        return _blocked(address, EgressBlockReason.PRIVATE)
    if isinstance(address, IPv4Address) and address in _SHARED_ADDRESS_SPACE:
        return _blocked(address, EgressBlockReason.SHARED)
    if address.is_reserved:
        return _blocked(address, EgressBlockReason.RESERVED)
    if address.is_private:
        return _blocked(address, EgressBlockReason.PRIVATE)
    if address.is_multicast:
        return _blocked(address, EgressBlockReason.MULTICAST)
    if any(address in network for network in _NON_GLOBAL_NETWORKS) and not any(
        address in network for network in _GLOBAL_EXCEPTIONS
    ):
        return _blocked(address, EgressBlockReason.NON_GLOBAL)
    if not address.is_global:
        return _blocked(address, EgressBlockReason.NON_GLOBAL)
    return AddressClassification(address=address, allowed=True)


def decide_public_egress(
    request: EgressRequest,
    *,
    resolver: AddressResolver | None = None,
) -> EgressDecision:
    """Resolve and evaluate one public-egress connection, failing closed.

    Resolution happens for every call.  If DNS returns both allowed and blocked
    addresses, the entire decision is denied rather than selecting the public
    answer.  An allowed result pins the first checked address for the connector.
    """

    if request.transport not in _PROXY_TRANSPORTS:
        return _denied(EgressBlockReason.UNSUPPORTED_TRANSPORT)

    hostname = _normalize_hostname(request.hostname)
    if hostname is None or not 1 <= request.port <= 65535:
        return _denied(EgressBlockReason.INVALID_TARGET)

    try:
        literal = ip_address(hostname)
    except ValueError:
        try:
            raw_addresses = (resolver or SocketAddressResolver()).resolve(hostname, request.port)
        except (OSError, ValueError):
            return _denied(EgressBlockReason.DNS_RESOLUTION_FAILED)
    else:
        raw_addresses = (str(literal),)

    classifications: list[AddressClassification] = []
    seen: set[IPAddress] = set()
    try:
        for raw_address in raw_addresses:
            address = ip_address(raw_address)
            if address in seen:
                continue
            seen.add(address)
            classifications.append(classify_public_address(address))
    except (TypeError, ValueError):
        return _denied(EgressBlockReason.DNS_RESOLUTION_FAILED)

    if not classifications:
        return _denied(EgressBlockReason.NO_ADDRESSES)

    checked = tuple(classifications)
    allowed = tuple(item for item in checked if item.allowed)
    blocked = tuple(item for item in checked if not item.allowed)
    if blocked:
        reason = (
            EgressBlockReason.MIXED_ADDRESS_SCOPE
            if allowed
            else blocked[0].block_reason or EgressBlockReason.NON_GLOBAL
        )
        return _denied(reason, classifications=checked)

    pinned = allowed[0].address
    return EgressDecision(
        allowed=True,
        endpoint=ResolvedPublicEndpoint(
            hostname=hostname,
            port=request.port,
            resolved_addresses=tuple(item.address for item in allowed),
            pinned_address=pinned,
        ),
        classifications=checked,
    )


def _normalize_hostname(value: str) -> str | None:
    hostname = value.strip().lower()
    if hostname.startswith("[") and hostname.endswith("]"):
        hostname = hostname[1:-1]
    if (
        not hostname
        or "%" in hostname
        or any(char.isspace() for char in hostname)
        or any(char in hostname for char in "/\\@?#\0")
    ):
        return None
    return hostname.rstrip(".") or None


def _blocked(address: IPAddress, reason: EgressBlockReason) -> AddressClassification:
    return AddressClassification(address=address, allowed=False, block_reason=reason)


def _denied(
    reason: EgressBlockReason,
    *,
    classifications: tuple[AddressClassification, ...] = (),
) -> EgressDecision:
    return EgressDecision(
        allowed=False,
        block_reason=reason,
        classifications=classifications,
    )
