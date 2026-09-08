"""Native policy configuration and trusted host placement (no privileged effects)."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from smolvm.network_policy import NetworkPolicy
from smolvm.network_policy import placement as host
from smolvm.types import VMConfig, WorkspaceMount
from smolvm.vm import SmolVMManager


def config(**updates):
    fields = {
        "vm_id": "sbx-policy",
        "backend": "qemu",
        "qemu_network": "tap",
        "comm_channel": "vsock",
        "kernel_path": "/missing/kernel",
        "rootfs_path": "/missing/rootfs",
        "network_policy": {"allowed_domains": ["Example.COM"]},
    }
    fields.update(updates)
    return VMConfig.model_validate(fields, context={"validate_paths": False})


def vm(**updates):
    return SimpleNamespace(
        vm_id="sbx-policy",
        config=config(**updates),
        network=SimpleNamespace(
            mode="nat",
            guest_ip="172.16.0.2",
            gateway_ip="172.16.0.1",
            tap_device="tap2",
        ),
    )


def test_policy_roundtrip_and_frozen_config():
    original = config()
    restored = VMConfig.model_validate_json(
        original.model_dump_json(), context={"validate_paths": False}
    )
    assert restored.network_policy.allowed_domains == ("example.com",)
    assert restored.network_policy.identity == original.network_policy.identity
    with pytest.raises(ValidationError):
        restored.network_policy = NetworkPolicy(allowed_domains=[])
    assert config(network_policy=None).network_policy is None
    assert config(network_policy={"allowed_domains": []}).network_policy.allowed_domains == ()


@pytest.mark.parametrize(
    "updates, message",
    [
        ({"backend": "firecracker"}, "backend='qemu'"),
        ({"backend": None}, "backend='qemu'"),
        ({"qemu_network": "slirp"}, "qemu_network='tap'"),
        ({"comm_channel": "ssh"}, "comm_channel='vsock'"),
        ({"comm_channel": None}, "comm_channel='vsock'"),
        ({"internet_settings": {}}, "not both"),
        ({"network_attachment": {"mode": "bridge", "bridge": "br0"}}, "NAT"),
    ],
)
def test_unsupported_configuration_rejected(updates, message):
    with pytest.raises(ValidationError, match=message):
        config(**updates)


def test_incomplete_native_integration_cannot_ignore_policy():
    SmolVMManager._require_native_policy_lifecycle(config(network_policy=None))
    with pytest.raises(Exception, match="not yet available"):
        SmolVMManager._require_native_policy_lifecycle(config())


@pytest.fixture
def discovery(monkeypatch):
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/sys/net/ipv4/ip_local_port_range":
            return "32768 60999"
        if str(path) == "/etc/resolv.conf":
            return "nameserver 127.0.0.53\nnameserver ::1\nnameserver 127.0.0.53\n"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(host.pwd, "getpwuid", Mock(side_effect=KeyError))
    monkeypatch.setattr(host.grp, "getgrgid", Mock(side_effect=KeyError))
    run = Mock(
        return_value=SimpleNamespace(
            stdout=json.dumps(
                [
                    {
                        "addr_info": [
                            {"family": "inet", "local": "172.16.0.1"},
                            {"family": "inet", "local": "192.168.1.2"},
                        ]
                    },
                ]
            )
        )
    )
    monkeypatch.setattr(host.subprocess, "run", run)
    return run


def test_placement_is_stable_and_host_global(discovery):
    first, path = host.placement(vm())
    second, second_path = host.placement(vm())
    assert first == second
    assert path == second_path == Path("/run/smolvm/network-policy/tap2.json")
    assert first.proxy_uid == 100002
    assert first.proxy_port == 61002
    assert first.resolver_addresses == ("127.0.0.53",)
    assert first.host_addresses == ("172.16.0.1", "192.168.1.2")
    assert first.platform_ports == ()


def test_empty_policy_has_no_proxy_identity_or_dns(discovery, monkeypatch):
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=AssertionError("no DNS needed")))
    binding, _ = host.placement(vm(network_policy={"allowed_domains": []}))
    assert binding.proxy_uid is binding.proxy_port is None
    assert binding.resolver_addresses == ()


@pytest.mark.parametrize(
    "ip,tap",
    [
        ("10.0.0.2", "tap2"),
        ("172.16.0.2", "tap3"),
        ("172.16.255.254", "tap65534"),
    ],
)
def test_invalid_lease_or_port_overflow_rejected(discovery, ip, tap):
    sandbox = vm()
    sandbox.network.guest_ip, sandbox.network.tap_device = ip, tap
    with pytest.raises(ValueError):
        host.placement(sandbox)


def test_ephemeral_port_overlap_rejected(discovery, monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: "32768 65535")
    with pytest.raises(RuntimeError, match="temporary port range"):
        host.placement(vm())


def test_host_account_uid_never_borrowed(discovery, monkeypatch):
    monkeypatch.setattr(host.pwd, "getpwuid", Mock(return_value=object()))
    with pytest.raises(RuntimeError, match="host account"):
        host.placement(vm())


def test_gateway_must_exist(discovery):
    discovery.return_value.stdout = "[]"
    with pytest.raises(RuntimeError, match="gateway address"):
        host.placement(vm())


@pytest.mark.parametrize(
    "folder",
    [
        "/",
        "/tmp",
        "/run",
        "/run/smolvm/network-policy/keys",
        "/tmp/smolvm-policy-secret/keys",
        "/var/lib/smolvm",
    ],
)
def test_shared_policy_storage_rejected(monkeypatch, folder):
    monkeypatch.setattr(host, "require_runtime", Mock())
    monkeypatch.setattr(host.os, "geteuid", lambda: 0)
    monkeypatch.setattr(host.tempfile, "gettempdir", lambda: "/tmp")
    mount = WorkspaceMount.model_construct(host_path=Path(folder))
    sandbox = vm(workspace_mounts=[mount])
    with pytest.raises(ValueError, match="policy files|worker files"):
        host.validate_host(sandbox, Path("/var/lib/smolvm"))


def test_specific_temporary_workspace_allowed(monkeypatch):
    monkeypatch.setattr(host, "require_runtime", Mock())
    monkeypatch.setattr(host.os, "geteuid", lambda: 0)
    monkeypatch.setattr(host.tempfile, "gettempdir", lambda: "/tmp")
    sandbox = vm(workspace_mounts=[WorkspaceMount.model_construct(host_path=Path("/tmp/project"))])
    host.validate_host(sandbox, Path("/var/lib/smolvm"))


def test_unprivileged_launch_rejected_without_sudo(monkeypatch):
    monkeypatch.setattr(host, "require_runtime", Mock())
    monkeypatch.setattr(host.os, "geteuid", lambda: 1000)
    with pytest.raises(RuntimeError, match="root-owned"):
        host.validate_host(vm(), Path("/var/lib/smolvm"))


def test_model_copy_cannot_bypass_runtime_validation(monkeypatch):
    sandbox = vm()
    sandbox.config = sandbox.config.model_copy(update={"qemu_network": "slirp"})
    with pytest.raises(ValidationError, match="qemu_network='tap'"):
        host.validate_host(sandbox, Path("/var/lib/smolvm"))


def test_adoption_uses_saved_discovery(discovery, monkeypatch, tmp_path):
    from dataclasses import asdict

    from smolvm.network_policy import lifecycle

    sandbox = vm()
    binding, _ = host.placement(sandbox)
    monkeypatch.setattr(host, "STATE_DIRECTORY", tmp_path)
    (tmp_path / "tap2.json").write_text(json.dumps({"binding": asdict(binding)}))
    check = Mock(return_value={"status": "active"})
    monkeypatch.setattr(lifecycle, "policy_status", check)
    discovery.side_effect = AssertionError("Must not rediscover or repair")
    adopted, state = host.active_policy(sandbox, 1234)
    assert adopted == binding
    assert state["status"] == "active"
    check.assert_called_once_with(
        tmp_path / "tap2.json", sandbox.config.network_policy, binding, 1234
    )
    check.return_value = None
    with pytest.raises(RuntimeError, match="sbx-policy"):
        host.active_policy(sandbox, 1234)


@pytest.mark.parametrize(
    "field,value",
    [
        ("guest_ip", "172.16.0.3"),
        ("proxy_uid", 100003),
        ("proxy_port", 61003),
        ("platform_ports", [8444]),
    ],
)
def test_adoption_rejects_changed_binding(discovery, monkeypatch, tmp_path, field, value):
    from dataclasses import asdict

    from smolvm.network_policy import lifecycle

    sandbox = vm()
    binding, _ = host.placement(sandbox)
    saved = asdict(binding)
    saved[field] = value
    monkeypatch.setattr(host, "STATE_DIRECTORY", tmp_path)
    (tmp_path / "tap2.json").write_text(json.dumps({"binding": saved}))
    check = Mock()
    monkeypatch.setattr(lifecycle, "policy_status", check)
    with pytest.raises(RuntimeError, match="could not be verified"):
        host.active_policy(sandbox, 1234)
    check.assert_not_called()
