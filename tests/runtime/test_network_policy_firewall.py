"""Ownership/rendering tests; real packet coverage lives in the Linux harness."""

from dataclasses import FrozenInstanceError

import pytest

from smolvm.network_policy import firewall
from smolvm.network_policy.firewall import NetworkBinding


def binding(**overrides):
    fields = {
        "tap": "tap1",
        "guest_ip": "172.16.0.2",
        "gateway_ip": "172.16.0.1",
        "host_addresses": ("172.16.0.1", "8.8.4.4"),
        "proxy_uid": 100001,
        "proxy_port": 18080,
    }
    fields.update(overrides)
    return NetworkBinding(**fields)


@pytest.mark.parametrize(
    "override",
    [
        {"tap": 'tap1"; flush ruleset'},
        {"tap": "a" * 16},
        {"guest_ip": "127.1"},
        {"gateway_ip": "::1"},
        {"host_addresses": ()},
        {"resolver_addresses": ("8.8.8.8; flush ruleset",)},
        {"proxy_uid": 0},
        {"proxy_uid": None},
        {"proxy_port": None},
        {"proxy_port": 65536},
        {"proxy_port": True},
        {"platform_ports": (18080,)},
        {"platform_ports": ("443; flush ruleset",)},
    ],
)
def test_invalid_bindings_never_render_commands(override):
    with pytest.raises(ValueError):
        binding(**override)


def test_no_shared_table_or_default_platform_hole():
    first, second = binding(), binding(tap="tap2")
    assert first.table != second.table
    assert first.table != binding(tap="tap-1").table
    assert "8444" not in first.rules()
    assert "elements" not in first.rules().split("set admitted_ports")[1].split("}")[0]
    assert "meta nfproto ipv6" in first.rules()
    assert "meta skuid 100001" in first.rules()


def test_deny_all_has_no_worker_rules_or_admission():
    policy = binding(proxy_uid=None, proxy_port=None)
    assert "chain proxy_output" not in policy.rules()
    assert "@admitted_ports" in policy.rules()
    with pytest.raises(ValueError, match="deny-all"):
        policy.admit()


def test_defensive_immutable_copy_of_binding_arrays():
    addresses = ["172.16.0.1"]
    policy = binding(host_addresses=addresses)
    addresses.append("9.9.9.9")
    assert policy.host_addresses == ("172.16.0.1",)
    with pytest.raises(FrozenInstanceError):
        policy.tap = "tap2"


def test_replacement_is_one_atomic_transaction_for_only_its_table(monkeypatch):
    policy = binding()
    calls = []

    def nft(*args, script=None):
        calls.append((args, script))
        if args == ("-j", "list", "tables"):
            return '{"nftables":[{"table":{"family":"inet","name":"' + policy.table + '"}}]}'
        return ""

    monkeypatch.setattr(firewall, "_nft", nft)
    policy.install()
    assert len(calls) == 2
    assert calls[1] == (("-f", "-"), f"delete table inet {policy.table}\n" + policy.rules())
    assert "flush ruleset" not in calls[1][1]


def test_inventory_failure_does_not_attempt_replacement(monkeypatch):
    calls = []

    def nft(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("injected nft failure")

    monkeypatch.setattr(firewall, "_nft", nft)
    with pytest.raises(RuntimeError):
        binding().install()
    assert calls == [("-j", "list", "tables")]
