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

"""Contract tests for the internal public-egress policy boundary."""

from __future__ import annotations

import json
import socket
from collections.abc import Sequence
from pathlib import Path

import pytest

from smolvm.host._public_egress import (
    EgressBlockReason,
    EgressRequest,
    EgressRequestKind,
    EgressTransport,
    classify_public_address,
    decide_public_egress,
)

_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "public_egress_contract.json"


class ScriptedResolver:
    """Return deterministic answers and record each connection-time lookup."""

    def __init__(self, answers: Sequence[Sequence[str]]) -> None:
        self._answers = iter(answers)
        self.calls: list[tuple[str, int]] = []

    def resolve(self, hostname: str, port: int) -> Sequence[str]:
        self.calls.append((hostname, port))
        return next(self._answers)


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("0.0.0.0", EgressBlockReason.UNSPECIFIED),
        ("127.0.0.1", EgressBlockReason.LOOPBACK),
        ("169.254.1.2", EgressBlockReason.LINK_LOCAL),
        ("10.0.0.1", EgressBlockReason.PRIVATE),
        ("172.16.0.1", EgressBlockReason.PRIVATE),
        ("192.168.0.1", EgressBlockReason.PRIVATE),
        ("100.64.0.1", EgressBlockReason.SHARED),
        ("224.0.0.1", EgressBlockReason.MULTICAST),
        ("240.0.0.1", EgressBlockReason.RESERVED),
        ("169.254.169.254", EgressBlockReason.NETWORK_METADATA),
        ("100.100.100.200", EgressBlockReason.NETWORK_METADATA),
        ("::", EgressBlockReason.UNSPECIFIED),
        ("::1", EgressBlockReason.LOOPBACK),
        ("fe80::1", EgressBlockReason.LINK_LOCAL),
        ("fec0::1", EgressBlockReason.PRIVATE),
        ("feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", EgressBlockReason.PRIVATE),
        ("fc00::1", EgressBlockReason.PRIVATE),
        ("ff02::1", EgressBlockReason.MULTICAST),
        ("fd00:ec2::254", EgressBlockReason.NETWORK_METADATA),
        ("::ffff:8.8.8.8", EgressBlockReason.IPV4_MAPPED),
        ("::ffff:127.0.0.1", EgressBlockReason.IPV4_MAPPED),
    ],
)
def test_special_addresses_are_denied(address: str, reason: EgressBlockReason) -> None:
    classification = classify_public_address(address)
    assert classification.allowed is False
    assert classification.block_reason is reason


@pytest.mark.parametrize("address", ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"])
def test_global_addresses_are_allowed(address: str) -> None:
    classification = classify_public_address(address)
    assert classification.allowed is True
    assert classification.block_reason is None


@pytest.mark.parametrize(
    "address",
    [
        "192.0.0.8",
        "192.0.2.1",
        "192.88.99.1",
        "198.18.0.1",
        "198.51.100.1",
        "203.0.113.1",
        "64:ff9b:1::1",
        "100::1",
        "2001:db8::1",
        "2002:c000:0201::1",
    ],
)
def test_version_sensitive_special_ranges_are_always_denied(address: str) -> None:
    assert classify_public_address(address).allowed is False


@pytest.mark.parametrize(
    ("denied_upper_bound", "adjacent_public_address"),
    [
        ("198.19.255.255", "198.20.0.1"),
        ("198.51.100.255", "198.51.101.1"),
        ("203.0.113.255", "203.0.114.1"),
    ],
)
def test_special_range_upper_bounds_do_not_block_adjacent_public_addresses(
    denied_upper_bound: str,
    adjacent_public_address: str,
) -> None:
    assert classify_public_address(denied_upper_bound).allowed is False
    assert classify_public_address(adjacent_public_address).allowed is True


def test_every_denial_fixture_has_one_matching_public_control() -> None:
    fixtures = json.loads(_FIXTURE_PATH.read_text())
    names = [fixture["name"] for fixture in fixtures]
    assert len(names) == len(set(names))

    denials = {fixture["name"]: fixture for fixture in fixtures if not fixture["allowed"]}
    controls: dict[str, list[dict]] = {}
    for fixture in fixtures:
        control_for = fixture.get("control_for")
        if control_for is not None:
            assert fixture["allowed"] is True
            controls.setdefault(control_for, []).append(fixture)

    assert set(controls) == set(denials)
    for denial_name, denial in denials.items():
        assert len(controls[denial_name]) == 1
        control = controls[denial_name][0]
        assert control["kind"] == denial["kind"]
        assert control["hostname"] == denial["hostname"]
        if denial["transport"] in {"direct_dns", "raw_socket"}:
            assert control["transport"] in {"http", "https", "websocket", "websocket_secure"}
        else:
            assert control["transport"] == denial["transport"]
        assert all(classify_public_address(answer).allowed for answer in control["answers"])


def test_contract_attack_fixtures() -> None:
    fixtures = json.loads(_FIXTURE_PATH.read_text())
    for fixture in fixtures:
        resolver = ScriptedResolver([fixture["answers"]])
        decision = decide_public_egress(
            EgressRequest(
                hostname=fixture["hostname"],
                port=443,
                transport=EgressTransport(fixture["transport"]),
                kind=EgressRequestKind(fixture["kind"]),
            ),
            resolver=resolver,
        )
        assert decision.allowed is fixture["allowed"], fixture["name"]
        if "reason" in fixture:
            assert decision.block_reason is EgressBlockReason(fixture["reason"]), fixture["name"]
        if decision.allowed:
            assert decision.endpoint is not None
            assert str(decision.endpoint.pinned_address) == fixture["answers"][0]
        if fixture["transport"] in {"direct_dns", "raw_socket"}:
            assert resolver.calls == [], fixture["name"]
        else:
            assert resolver.calls == [(fixture["hostname"], 443)], fixture["name"]


def test_dns_is_resolved_for_each_connection_and_rebinding_fails_closed() -> None:
    resolver = ScriptedResolver([["1.1.1.1"], ["10.0.0.7"]])
    request = EgressRequest(
        hostname="changes.test",
        port=443,
        transport=EgressTransport.HTTPS,
        kind=EgressRequestKind.NAVIGATION,
    )

    first = decide_public_egress(request, resolver=resolver)
    second = decide_public_egress(request, resolver=resolver)

    assert first.allowed is True
    assert first.endpoint is not None
    assert str(first.endpoint.pinned_address) == "1.1.1.1"
    assert second.allowed is False
    assert second.block_reason is EgressBlockReason.PRIVATE
    assert resolver.calls == [("changes.test", 443), ("changes.test", 443)]


def test_public_answer_is_pinned_without_an_extra_lookup() -> None:
    resolver = ScriptedResolver([["2606:4700:4700::1111", "1.1.1.1"]])
    decision = decide_public_egress(
        EgressRequest(
            hostname="dual-stack.test.",
            port=443,
            transport=EgressTransport.HTTPS,
            kind=EgressRequestKind.SUBRESOURCE,
        ),
        resolver=resolver,
    )

    assert decision.allowed is True
    assert decision.endpoint is not None
    assert decision.endpoint.hostname == "dual-stack.test"
    assert str(decision.endpoint.pinned_address) == "2606:4700:4700::1111"
    assert tuple(map(str, decision.endpoint.resolved_addresses)) == (
        "2606:4700:4700::1111",
        "1.1.1.1",
    )
    assert resolver.calls == [("dual-stack.test", 443)]


@pytest.mark.parametrize(
    ("hostname", "port"),
    [("", 443), ("public.test", 0), ("public.test", 65536), ("fe80::1%en0", 443)],
)
def test_invalid_targets_fail_closed(hostname: str, port: int) -> None:
    decision = decide_public_egress(
        EgressRequest(
            hostname=hostname,
            port=port,
            transport=EgressTransport.HTTPS,
            kind=EgressRequestKind.NAVIGATION,
        ),
        resolver=ScriptedResolver([["1.1.1.1"]]),
    )
    assert decision.allowed is False
    assert decision.block_reason is EgressBlockReason.INVALID_TARGET


def test_empty_and_malformed_dns_answers_fail_closed() -> None:
    request = EgressRequest(
        hostname="broken.test",
        port=443,
        transport=EgressTransport.HTTPS,
        kind=EgressRequestKind.NAVIGATION,
    )
    empty = decide_public_egress(request, resolver=ScriptedResolver([[]]))
    malformed = decide_public_egress(request, resolver=ScriptedResolver([["not-an-ip"]]))
    assert empty.block_reason is EgressBlockReason.NO_ADDRESSES
    assert malformed.block_reason is EgressBlockReason.DNS_RESOLUTION_FAILED


def test_ip_literal_is_checked_without_dns() -> None:
    resolver = ScriptedResolver([["1.1.1.1"]])
    decision = decide_public_egress(
        EgressRequest(
            hostname="[::1]",
            port=80,
            transport=EgressTransport.HTTP,
            kind=EgressRequestKind.NAVIGATION,
        ),
        resolver=resolver,
    )
    assert decision.block_reason is EgressBlockReason.LOOPBACK
    assert resolver.calls == []


def test_default_resolver_uses_tcp_dual_stack_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_getaddrinfo(*args: object, **kwargs: object) -> list[tuple]:
        calls.append((*args, kwargs))
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:4700::1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    decision = decide_public_egress(
        EgressRequest(
            hostname="public.test",
            port=443,
            transport=EgressTransport.HTTPS,
            kind=EgressRequestKind.NAVIGATION,
        )
    )

    assert decision.allowed is True
    assert decision.endpoint is not None
    assert tuple(map(str, decision.endpoint.resolved_addresses)) == ("2606:4700::1", "1.1.1.1")
    assert calls == [
        (
            "public.test",
            443,
            {
                "family": socket.AF_UNSPEC,
                "type": socket.SOCK_STREAM,
                "proto": socket.IPPROTO_TCP,
            },
        )
    ]
