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

"""Tests for the fail-closed TAP-to-proxy policy."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from smolvm.host.network import NetworkManager


def test_public_proxy_policy_allows_only_the_owned_listener() -> None:
    script = NetworkManager()._public_proxy_policy_script(
        "tap42",
        guest_ip="172.16.0.42",
        proxy_host_ip="172.16.0.1",
        proxy_port=43128,
    )

    proxy_allow = (
        'input iifname "tap42" ip saddr 172.16.0.42 ip daddr 172.16.0.1 '
        "tcp dport 43128 ct state new,established counter accept"
    )
    input_drop = 'input iifname "tap42" counter drop'
    forward_drop = 'forward iifname "tap42" counter drop'
    assert proxy_allow in script
    assert input_drop in script
    assert forward_drop in script
    assert script.index(proxy_allow) < script.index(input_drop)
    assert 'forward iifname "tap42" counter accept' not in script
    assert "169.254" not in script  # The unconditional drops already cover it.


def test_apply_public_proxy_policy_is_one_transaction() -> None:
    network = NetworkManager()
    network._ensure_nftables_base = MagicMock()  # type: ignore[method-assign]
    network._run_nft_script = MagicMock()  # type: ignore[method-assign]

    network.apply_public_proxy_policy(
        "tap9",
        guest_ip="172.16.0.9",
        proxy_host_ip="172.16.0.1",
        proxy_port=3128,
    )

    network._ensure_nftables_base.assert_called_once_with()
    network._run_nft_script.assert_called_once()
    script = network._run_nft_script.call_args.args[0]
    assert script.startswith("add table inet smolvm_policy_tap9\nflush table")
    assert script.endswith('delete element inet smolvm_filter allowed_taps { "tap9" }\n')


@pytest.mark.parametrize(
    ("guest_ip", "proxy_ip", "port"),
    [
        ("not-an-ip", "172.16.0.1", 3128),
        ("172.16.0.2", "::1", 3128),
        ("172.16.0.2", "172.16.0.1", 0),
        ("172.16.0.2", "172.16.0.1", 65536),
    ],
)
def test_public_proxy_policy_rejects_invalid_endpoints(
    guest_ip: str,
    proxy_ip: str,
    port: int,
) -> None:
    with pytest.raises(ValueError):
        NetworkManager()._public_proxy_policy_script(
            "tap1",
            guest_ip=guest_ip,
            proxy_host_ip=proxy_ip,
            proxy_port=port,
        )
