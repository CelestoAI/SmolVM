"""Unit tests for bridged networking support."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from smolvm.types import (
    NetworkAttachmentConfig,
    NetworkConfig,
    VMConfig,
)


def _make_vm_config(
    tmp_path: Path,
    *,
    network_attachment: NetworkAttachmentConfig | None = None,
    comm_channel: str | None = None,
    workspace_mounts: list | None = None,
    port_forwards: list | None = None,
    internet_settings: object | None = None,
    vm_id: str = "test-vm",
) -> VMConfig:
    """Build a VMConfig with real tmp files for path validation."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()
    kwargs: dict = {
        "vm_id": vm_id,
        "kernel_path": kernel,
        "rootfs_path": rootfs,
    }
    if network_attachment is not None:
        kwargs["network_attachment"] = network_attachment
    if comm_channel is not None:
        kwargs["comm_channel"] = comm_channel
    if workspace_mounts is not None:
        kwargs["workspace_mounts"] = workspace_mounts
    if port_forwards is not None:
        kwargs["port_forwards"] = port_forwards
    if internet_settings is not None:
        kwargs["internet_settings"] = internet_settings
    return VMConfig(**kwargs)


def _make_vm_config_skip_paths(
    *,
    network_attachment: NetworkAttachmentConfig | None = None,
    comm_channel: str | None = None,
    workspace_mounts: list | None = None,
    port_forwards: list | None = None,
    internet_settings: object | None = None,
    vm_id: str = "test-vm",
) -> VMConfig:
    """Build a VMConfig skipping path validation (for tests that don't need files)."""
    data: dict = {
        "vm_id": vm_id,
        "kernel_path": "/dev/null",
        "rootfs_path": "/dev/null",
    }
    if network_attachment is not None:
        data["network_attachment"] = {
            "mode": network_attachment.mode,
            "bridge": network_attachment.bridge,
        }
    if comm_channel is not None:
        data["comm_channel"] = comm_channel
    if workspace_mounts is not None:
        data["workspace_mounts"] = workspace_mounts
    if port_forwards is not None:
        data["port_forwards"] = [
            {"host_port": pf.host_port, "guest_port": pf.guest_port}
            for pf in port_forwards
        ]
    if internet_settings is not None:
        data["internet_settings"] = {
            "allowed_domains": internet_settings.allowed_domains,
        }
    return VMConfig.model_validate(data, context={"validate_paths": False})


class TestNetworkAttachmentConfig:
    """Tests for the NetworkAttachmentConfig model."""

    def test_nat_default(self) -> None:
        na = NetworkAttachmentConfig()
        assert na.mode == "nat"
        assert na.bridge is None

    def test_bridge_with_name(self) -> None:
        na = NetworkAttachmentConfig(mode="bridge", bridge="br10")
        assert na.mode == "bridge"
        assert na.bridge == "br10"

    def test_bridge_requires_name(self) -> None:
        with pytest.raises(Exception, match="requires a bridge name"):
            NetworkAttachmentConfig(mode="bridge")

    def test_nat_rejects_bridge(self) -> None:
        with pytest.raises(Exception, match="does not accept a bridge name"):
            NetworkAttachmentConfig(mode="nat", bridge="br10")

    def test_bridge_name_empty_rejected(self) -> None:
        with pytest.raises(Exception, match="cannot be empty"):
            NetworkAttachmentConfig(mode="bridge", bridge="   ")

    def test_bridge_name_too_long(self) -> None:
        with pytest.raises(Exception, match="15 bytes or fewer"):
            NetworkAttachmentConfig(mode="bridge", bridge="a" * 16)

    def test_bridge_name_invalid_chars(self) -> None:
        with pytest.raises(Exception, match="not valid in a Linux interface name"):
            NetworkAttachmentConfig(mode="bridge", bridge="br bad")

    def test_frozen(self) -> None:
        na = NetworkAttachmentConfig()
        with pytest.raises(ValidationError):
            na.mode = "bridge"  # type: ignore[misc]


class TestNetworkConfigBridgeMode:
    """Tests for NetworkConfig in bridge mode."""

    def test_bridge_network_config(self) -> None:
        nc = NetworkConfig(
            mode="bridge",
            bridge="br10",
            tap_device="svmb1234",
            guest_mac="aa:fc:00:11:22:33",
        )
        assert nc.mode == "bridge"
        assert nc.bridge == "br10"
        assert nc.guest_ip is None
        assert nc.gateway_ip is None
        assert nc.netmask is None
        assert nc.ssh_host_port is None

    def test_bridge_rejects_guest_ip(self) -> None:
        with pytest.raises(Exception, match="must not set guest_ip"):
            NetworkConfig(
                mode="bridge",
                bridge="br10",
                tap_device="svmb1",
                guest_mac="aa:fc:00:11:22:33",
                guest_ip="10.0.0.5",
            )

    def test_bridge_rejects_gateway_ip(self) -> None:
        with pytest.raises(Exception, match="must not set gateway_ip"):
            NetworkConfig(
                mode="bridge",
                bridge="br10",
                tap_device="svmb1",
                guest_mac="aa:fc:00:11:22:33",
                gateway_ip="10.0.0.1",
            )

    def test_bridge_rejects_ssh_host_port(self) -> None:
        with pytest.raises(Exception, match="must not set ssh_host_port"):
            NetworkConfig(
                mode="bridge",
                bridge="br10",
                tap_device="svmb1",
                guest_mac="aa:fc:00:11:22:33",
                ssh_host_port=2200,
            )

    def test_nat_requires_guest_ip(self) -> None:
        with pytest.raises(Exception, match="requires guest_ip"):
            NetworkConfig(
                mode="nat",
                tap_device="tap0",
                guest_mac="aa:fc:00:11:22:33",
            )

    def test_nat_rejects_bridge(self) -> None:
        with pytest.raises(Exception, match="must not set bridge"):
            NetworkConfig(
                mode="nat",
                bridge="br10",
                tap_device="tap0",
                guest_mac="aa:fc:00:11:22:33",
                guest_ip="172.16.0.2",
            )

    def test_nat_defaults_filled(self) -> None:
        nc = NetworkConfig(
            guest_ip="172.16.0.2",
            tap_device="tap0",
            guest_mac="aa:fc:00:11:22:33",
        )
        assert nc.mode == "nat"
        assert nc.gateway_ip == "172.16.0.1"
        assert nc.netmask == "255.255.255.0"

    def test_old_json_compat(self) -> None:
        """Old JSON records without mode/bridge default to NAT."""
        old = json.dumps({
            "guest_ip": "172.16.0.2",
            "gateway_ip": "172.16.0.1",
            "netmask": "255.255.255.0",
            "tap_device": "tap0",
            "guest_mac": "aa:fc:00:11:22:33",
            "ssh_host_port": 2200,
        })
        nc = NetworkConfig.model_validate_json(old)
        assert nc.mode == "nat"
        assert nc.bridge is None


class TestVMConfigBridgeMode:
    """Tests for VMConfig bridge mode validation."""

    def test_default_network_attachment_is_nat(self, tmp_path: Path) -> None:
        config = _make_vm_config(tmp_path)
        assert config.network_attachment.mode == "nat"

    def test_bridge_mode_rejects_ssh_comm_channel(self, tmp_path: Path) -> None:
        with pytest.raises(Exception, match="requires vsock"):
            _make_vm_config(
                tmp_path,
                comm_channel="ssh",
                network_attachment=NetworkAttachmentConfig(mode="bridge", bridge="br10"),
            )

    def test_bridge_mode_rejects_workspace_mounts(self, tmp_path: Path) -> None:
        with pytest.raises(Exception, match="Workspace mounts are not supported"):
            _make_vm_config_skip_paths(
                network_attachment=NetworkAttachmentConfig(mode="bridge", bridge="br10"),
                workspace_mounts=[{"host_path": "/tmp", "guest_path": "/workspace"}],
            )

    def test_bridge_mode_rejects_port_forwards(self, tmp_path: Path) -> None:
        from smolvm.types import PortForwardConfig

        with pytest.raises(Exception, match="Port forwards are not supported"):
            _make_vm_config_skip_paths(
                network_attachment=NetworkAttachmentConfig(mode="bridge", bridge="br10"),
                port_forwards=[PortForwardConfig(host_port=8080, guest_port=80)],
            )

    def test_bridge_mode_rejects_domain_allowlist(self, tmp_path: Path) -> None:
        from smolvm.types import InternetSettings

        with pytest.raises(Exception, match="Domain allow-lists are not enforced"):
            _make_vm_config_skip_paths(
                network_attachment=NetworkAttachmentConfig(mode="bridge", bridge="br10"),
                internet_settings=InternetSettings(allowed_domains=["example.com"]),
            )

    def test_nat_mode_accepts_normal_options(self, tmp_path: Path) -> None:
        config = _make_vm_config(tmp_path, comm_channel="ssh")
        assert config.network_attachment.mode == "nat"


class TestBridgeInspection:
    """Tests for BridgeInspection and inspect_bridge."""

    def test_bridge_inspection_dataclass(self) -> None:
        from smolvm.host.network import BridgeInspection

        ok = BridgeInspection(bridge_name="br10", ok=True)
        assert ok.ok is True
        assert ok.reason == ""

        bad = BridgeInspection(bridge_name="br10", ok=False, reason="not a bridge")
        assert bad.ok is False
        assert bad.reason == "not a bridge"

    def test_inspect_bridge_non_linux(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        with patch("platform.system", return_value="Darwin"):
            result = nm.inspect_bridge("br10")
        assert result.ok is False
        assert "Linux" in result.reason

    def test_inspect_bridge_missing(self) -> None:
        from smolvm.exceptions import SmolVMError
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        with patch("platform.system", return_value="Linux"), \
             patch("smolvm.host.network.run_command", side_effect=SmolVMError("not found")):
            result = nm.inspect_bridge("br10")
        assert result.ok is False
        assert "does not exist" in result.reason

    def test_inspect_bridge_wrong_type(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        mock_response = MagicMock()
        mock_response.stdout = json.dumps([{"link_type": "vlan", "flags": ["UP"]}])
        with patch("platform.system", return_value="Linux"), \
             patch("smolvm.host.network.run_command", return_value=mock_response):
            result = nm.inspect_bridge("eno1.10")
        assert result.ok is False
        assert "not a bridge" in result.reason

    def test_inspect_bridge_not_up(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        mock_response = MagicMock()
        mock_response.stdout = json.dumps([{"link_type": "bridge", "flags": []}])
        with patch("platform.system", return_value="Linux"), \
             patch("smolvm.host.network.run_command", return_value=mock_response):
            result = nm.inspect_bridge("br10")
        assert result.ok is False
        assert "not up" in result.reason

    def test_inspect_bridge_no_members(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        link_response = MagicMock()
        link_response.stdout = json.dumps([{"link_type": "bridge", "flags": ["UP", "LOWER_UP"]}])
        members_response = MagicMock()
        members_response.stdout = "[]"
        with patch("platform.system", return_value="Linux"), \
             patch(
                 "smolvm.host.network.run_command",
                 side_effect=[link_response, members_response],
             ):
            result = nm.inspect_bridge("br10")
        assert result.ok is False
        assert "no member" in result.reason.lower()

    def test_inspect_bridge_valid(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        link_response = MagicMock()
        link_response.stdout = json.dumps([{"link_type": "bridge", "flags": ["UP", "LOWER_UP"]}])
        members_response = MagicMock()
        members_response.stdout = json.dumps([{"ifname": "eno1.10"}])
        empty_addr = MagicMock()
        empty_addr.stdout = json.dumps([{"addr_info": []}])
        # inspect_bridge: 1 (link show) + 1 (members) + _check_bridge_addresses:
        #   bridge: 2 (inet, inet6) + member: 2 (inet, inet6) = 6 total
        with patch("platform.system", return_value="Linux"), \
             patch("smolvm.host.network.run_command",
                   side_effect=[link_response, members_response,
                                empty_addr, empty_addr, empty_addr, empty_addr]):
            result = nm.inspect_bridge("br10")
        assert result.ok is True

    def test_inspect_bridge_with_host_address(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        link_response = MagicMock()
        link_response.stdout = json.dumps([{"link_type": "bridge", "flags": ["UP", "LOWER_UP"]}])
        members_response = MagicMock()
        members_response.stdout = json.dumps([{"ifname": "eno1.10"}])
        addr_response = MagicMock()
        addr_response.stdout = json.dumps([{
            "addr_info": [{"local": "192.168.10.2", "scope": "global"}]
        }])
        with patch("platform.system", return_value="Linux"), \
             patch("smolvm.host.network.run_command",
                   side_effect=[link_response, members_response, addr_response, addr_response]):
            result = nm.inspect_bridge("br10")
        assert result.ok is False
        assert "192.168.10.2" in result.reason


class TestBridgeMacGeneration:
    """Tests for bridge MAC address generation."""

    def test_generate_bridge_mac_format(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        mac = nm.generate_bridge_mac()
        parts = mac.split(":")
        assert len(parts) == 6
        # Locally administered unicast (AA:FC prefix)
        assert parts[0].upper() == "AA"
        assert parts[1].upper() == "FC"

    def test_generate_bridge_mac_unique(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        macs = {nm.generate_bridge_mac() for _ in range(100)}
        assert len(macs) > 90  # Allow a few collisions in 100 random MACs


class TestTapAllocation:
    """Tests for TAP name reservation in storage."""

    def test_reserve_tap_name_bridge(self, tmp_path: Path) -> None:
        from smolvm.storage._sqlite import SQLiteStateManager

        manager = SQLiteStateManager(tmp_path / "test.db")
        config = _make_vm_config(tmp_path, vm_id="test-vm-1")
        manager.create_vm(config)
        tap = manager.reserve_tap_name("test-vm-1", mode="bridge", bridge_name="br10")
        assert tap.startswith("svmb")
        assert len(tap) <= 15

    def test_reserve_tap_name_idempotent(self, tmp_path: Path) -> None:
        from smolvm.storage._sqlite import SQLiteStateManager

        manager = SQLiteStateManager(tmp_path / "test.db")
        config = _make_vm_config(tmp_path, vm_id="test-vm-2")
        manager.create_vm(config)
        tap1 = manager.reserve_tap_name("test-vm-2", mode="bridge", bridge_name="br10")
        tap2 = manager.reserve_tap_name("test-vm-2", mode="bridge", bridge_name="br10")
        assert tap1 == tap2

    def test_get_tap_allocation(self, tmp_path: Path) -> None:
        from smolvm.storage._sqlite import SQLiteStateManager

        manager = SQLiteStateManager(tmp_path / "test.db")
        config = _make_vm_config(tmp_path, vm_id="test-vm-3")
        manager.create_vm(config)
        manager.reserve_tap_name("test-vm-3", mode="bridge", bridge_name="br10")
        alloc = manager.get_tap_allocation("test-vm-3")
        assert alloc is not None
        assert alloc[1] == "bridge"
        assert alloc[2] == "br10"

    def test_get_tap_allocation_none(self, tmp_path: Path) -> None:
        from smolvm.storage._sqlite import SQLiteStateManager

        manager = SQLiteStateManager(tmp_path / "test.db")
        assert manager.get_tap_allocation("nonexistent") is None

    def test_release_tap_name(self, tmp_path: Path) -> None:
        from smolvm.storage._sqlite import SQLiteStateManager

        manager = SQLiteStateManager(tmp_path / "test.db")
        config = _make_vm_config(tmp_path, vm_id="test-vm-4")
        manager.create_vm(config)
        manager.reserve_tap_name("test-vm-4", mode="bridge", bridge_name="br10")
        manager.release_tap_name("test-vm-4")
        assert manager.get_tap_allocation("test-vm-4") is None

    def test_reserve_tap_name_requested(self, tmp_path: Path) -> None:
        from smolvm.storage._sqlite import SQLiteStateManager

        manager = SQLiteStateManager(tmp_path / "test.db")
        config = _make_vm_config(tmp_path, vm_id="test-vm-5")
        manager.create_vm(config)
        tap = manager.reserve_tap_name(
            "test-vm-5", mode="bridge", bridge_name="br10", requested_tap="svmb9999"
        )
        assert tap == "svmb9999"

    def test_reserve_tap_name_conflict(self, tmp_path: Path) -> None:
        from smolvm.exceptions import NetworkError
        from smolvm.storage._sqlite import SQLiteStateManager

        manager = SQLiteStateManager(tmp_path / "test.db")
        config1 = _make_vm_config(tmp_path, vm_id="test-vm-6")
        config2 = _make_vm_config(tmp_path, vm_id="test-vm-7")
        manager.create_vm(config1)
        manager.create_vm(config2)
        manager.reserve_tap_name("test-vm-6", mode="bridge", requested_tap="svmbconf1")
        with pytest.raises(NetworkError, match="already reserved"):
            manager.reserve_tap_name("test-vm-7", mode="bridge", requested_tap="svmbconf1")


class TestQemuArgsBridgeMode:
    """Tests for QEMU args in bridge mode."""

    def test_bridge_mode_selects_tap_transport(self, tmp_path: Path) -> None:
        """QEMU should use TAP transport for bridge mode even with qemu_network='slirp'."""
        from smolvm.runtime.guest_platforms import _LINUX_SPEC
        from smolvm.runtime.qemu_args import build_qemu_argv
        from smolvm.types import VMInfo, VMState

        config = _make_vm_config(
            tmp_path,
            vm_id="test-bridge",
            network_attachment=NetworkAttachmentConfig(mode="bridge", bridge="br10"),
        )
        config = config.model_copy(update={"qemu_network": "slirp"})
        network = NetworkConfig(
            mode="bridge",
            bridge="br10",
            tap_device="svmb1234",
            guest_mac="aa:fc:00:11:22:33",
        )
        vm_info = VMInfo(
            vm_id="test-bridge",
            status=VMState.CREATED,
            config=config,
            network=network,
        )
        args = build_qemu_argv(
            vm_info,
            qemu_bin=Path("/usr/bin/qemu-system-x86_64"),
            boot_args=vm_info.config.boot_args,
            platform_spec=_LINUX_SPEC,
            host_system="Linux",
        )
        netdev_args = [a for a in args if a.startswith("tap,id=net0")]
        assert len(netdev_args) == 1
        assert "svmb1234" in netdev_args[0]
