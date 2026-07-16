"""Unit tests for bridged networking support."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

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
        if network_attachment.mode == "bridge":
            kwargs["guest_managed_networking"] = True
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
        if network_attachment.mode == "bridge":
            data["guest_managed_networking"] = True
    if comm_channel is not None:
        data["comm_channel"] = comm_channel
    if workspace_mounts is not None:
        data["workspace_mounts"] = workspace_mounts
    if port_forwards is not None:
        data["port_forwards"] = [
            {"host_port": pf.host_port, "guest_port": pf.guest_port} for pf in port_forwards
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

    @pytest.mark.parametrize("explicit_nulls", [False, True])
    def test_nat_defaults_filled(self, explicit_nulls: bool) -> None:
        values: dict[str, object] = {
            "guest_ip": "172.16.0.2",
            "tap_device": "tap0",
            "guest_mac": "aa:fc:00:11:22:33",
        }
        if explicit_nulls:
            values.update({"gateway_ip": None, "netmask": None})
        nc = NetworkConfig(**values)
        assert nc.mode == "nat"
        assert nc.gateway_ip == "172.16.0.1"
        assert nc.netmask == "255.255.255.0"

    def test_old_json_compat(self) -> None:
        """Old JSON records without mode/bridge default to NAT."""
        old = json.dumps(
            {
                "guest_ip": "172.16.0.2",
                "gateway_ip": "172.16.0.1",
                "netmask": "255.255.255.0",
                "tap_device": "tap0",
                "guest_mac": "aa:fc:00:11:22:33",
                "ssh_host_port": 2200,
            }
        )
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
        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "smolvm.host.network.run_command",
                side_effect=SmolVMError('Device "br10" does not exist.'),
            ),
        ):
            result = nm.inspect_bridge("br10")
        assert result.ok is False
        assert "does not exist" in result.reason

    def test_inspect_bridge_propagates_link_probe_failure(self) -> None:
        from smolvm.exceptions import SmolVMError
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "smolvm.host.network.run_command",
                side_effect=SmolVMError("ip command timed out"),
            ),
            pytest.raises(SmolVMError, match="timed out"),
        ):
            nm.inspect_bridge("br10")

    def test_inspect_bridge_propagates_member_probe_failure(self) -> None:
        from smolvm.exceptions import SmolVMError
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        bridge = MagicMock()
        bridge.stdout = json.dumps([{"linkinfo": {"info_kind": "bridge"}, "flags": ["UP"]}])
        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "smolvm.host.network.run_command",
                side_effect=[bridge, SmolVMError("member probe failed")],
            ),
            pytest.raises(SmolVMError, match="member probe failed"),
        ):
            nm.inspect_bridge("br10")

    def test_inspect_bridge_propagates_address_probe_failure(self) -> None:
        from smolvm.exceptions import SmolVMError
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        bridge = MagicMock()
        bridge.stdout = json.dumps([{"linkinfo": {"info_kind": "bridge"}, "flags": ["UP"]}])
        members = MagicMock()
        members.stdout = json.dumps([{"ifname": "eno1"}])
        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "smolvm.host.network.run_command",
                side_effect=[bridge, members, SmolVMError("address probe failed")],
            ),
            pytest.raises(SmolVMError, match="address probe failed"),
        ):
            nm.inspect_bridge("br10")

    def test_inspect_bridge_wrong_type(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        mock_response = MagicMock()
        mock_response.stdout = json.dumps(
            [{"link_type": "ether", "linkinfo": {"info_kind": "vlan"}, "flags": ["UP"]}]
        )
        with (
            patch("platform.system", return_value="Linux"),
            patch("smolvm.host.network.run_command", return_value=mock_response),
        ):
            result = nm.inspect_bridge("eno1.10")
        assert result.ok is False
        assert "not a bridge" in result.reason

    def test_inspect_bridge_not_up(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        mock_response = MagicMock()
        mock_response.stdout = json.dumps(
            [{"link_type": "ether", "linkinfo": {"info_kind": "bridge"}, "flags": []}]
        )
        with (
            patch("platform.system", return_value="Linux"),
            patch("smolvm.host.network.run_command", return_value=mock_response),
        ):
            result = nm.inspect_bridge("br10")
        assert result.ok is False
        assert "not active" in result.reason

    def test_inspect_bridge_no_members(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        link_response = MagicMock()
        link_response.stdout = json.dumps(
            [
                {
                    "link_type": "ether",
                    "linkinfo": {"info_kind": "bridge"},
                    "flags": ["UP", "LOWER_UP"],
                }
            ]
        )
        members_response = MagicMock()
        members_response.stdout = "[]"
        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "smolvm.host.network.run_command",
                side_effect=[link_response, members_response],
            ),
        ):
            result = nm.inspect_bridge("br10")
        assert result.ok is False
        assert "not connected" in result.reason.lower()

    def test_inspect_bridge_does_not_trust_tap_name_prefix(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        bridge_info = {
            "linkinfo": {"info_kind": "bridge"},
            "flags": ["UP"],
        }
        user_veth_info = {
            "ifname": "svmb-uplink",
            "linkinfo": {"info_kind": "veth"},
        }
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(nm, "_get_link_info", side_effect=[bridge_info, user_veth_info]),
            patch.object(nm, "_get_bridge_members", return_value=["svmb-uplink"]),
            patch.object(nm, "_check_bridge_addresses", return_value=""),
        ):
            result = nm.inspect_bridge("br10")

        assert result.ok is True

    def test_inspect_bridge_does_not_count_owned_tap_as_external_member(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        bridge_info = {
            "linkinfo": {"info_kind": "bridge"},
            "flags": ["UP"],
        }
        owned_tap_info = {
            "ifname": "svmb12345678",
            "ifalias": "smolvm-bridge:vm001",
            "linkinfo": {"info_kind": "tun", "info_data": {"type": "tap"}},
        }
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(nm, "_get_link_info", side_effect=[bridge_info, owned_tap_info]),
            patch.object(nm, "_get_bridge_members", return_value=["svmb12345678"]),
        ):
            result = nm.inspect_bridge("br10")

        assert result.ok is False
        assert "not connected" in result.reason

    def test_inspect_bridge_valid(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        link_response = MagicMock()
        link_response.stdout = json.dumps(
            [
                {
                    "link_type": "ether",
                    "linkinfo": {"info_kind": "bridge"},
                    "flags": ["UP", "LOWER_UP"],
                }
            ]
        )
        members_response = MagicMock()
        members_response.stdout = json.dumps([{"ifname": "eno1.10"}])
        empty_addr = MagicMock()
        empty_addr.stdout = json.dumps([{"addr_info": []}])
        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "smolvm.host.network.run_command",
                side_effect=[link_response, members_response, empty_addr, empty_addr],
            ) as run_command,
        ):
            result = nm.inspect_bridge("br10")
        assert result.ok is True
        assert all(call.kwargs["use_sudo"] is False for call in run_command.call_args_list)
        assert all("show" in call.args[0] for call in run_command.call_args_list)

    def test_inspect_bridge_with_host_address(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        link_response = MagicMock()
        link_response.stdout = json.dumps(
            [
                {
                    "link_type": "ether",
                    "linkinfo": {"info_kind": "bridge"},
                    "flags": ["UP", "LOWER_UP"],
                }
            ]
        )
        members_response = MagicMock()
        members_response.stdout = json.dumps([{"ifname": "eno1.10"}])
        addr_response = MagicMock()
        addr_response.stdout = json.dumps(
            [{"addr_info": [{"local": "192.168.10.2", "scope": "global"}]}]
        )
        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "smolvm.host.network.run_command",
                side_effect=[link_response, members_response, addr_response],
            ),
        ):
            result = nm.inspect_bridge("br10")
        assert result.ok is False
        assert "192.168.10.2" in result.reason
        assert "addr flush" not in result.reason

    def test_inspect_bridge_rejects_ipv6_link_local_address(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        link_response = MagicMock()
        link_response.stdout = json.dumps(
            [
                {
                    "link_type": "ether",
                    "linkinfo": {"info_kind": "bridge"},
                    "flags": ["UP"],
                }
            ]
        )
        members_response = MagicMock()
        members_response.stdout = json.dumps([{"ifname": "eno1.10"}])
        address_response = MagicMock()
        address_response.stdout = json.dumps(
            [{"addr_info": [{"local": "fe80::1234", "scope": "link"}]}]
        )
        with (
            patch("platform.system", return_value="Linux"),
            patch(
                "smolvm.host.network.run_command",
                side_effect=[link_response, members_response, address_response],
            ),
        ):
            result = nm.inspect_bridge("br10")

        assert result.ok is False
        assert "fe80::1234" in result.reason


class TestBridgedTapOwnership:
    """Tests for bridge TAP ownership and safe reuse."""

    @staticmethod
    def _tap_info(*, alias: str, master: str | None = None) -> dict[str, object]:
        info: dict[str, object] = {
            "ifname": "svmb1234",
            "ifalias": alias,
            "linkinfo": {"info_kind": "tun", "info_data": {"type": "tap"}},
        }
        if master is not None:
            info["master"] = master
        return info

    def test_prepare_new_tap_marks_ownership_before_attaching(self) -> None:
        from smolvm.host.network import BridgeInspection, NetworkManager

        nm = NetworkManager()
        with (
            patch.object(
                nm,
                "inspect_bridge",
                return_value=BridgeInspection("br10", True),
            ),
            patch.object(nm, "create_tap", return_value=True),
            patch.object(
                nm,
                "_get_link_info",
                side_effect=[
                    None,
                    self._tap_info(alias=""),
                    self._tap_info(alias="smolvm-bridge:vm001", master="br10"),
                ],
            ),
            patch.object(nm, "_set_tap_master") as set_master,
            patch("smolvm.host.network.run_command") as run_command,
        ):
            nm.prepare_bridged_tap("svmb1234", "br10", "vm001", user="alice")

        set_master.assert_called_once_with("svmb1234", "br10")
        assert run_command.call_args_list[0].args[0] == [
            "ip",
            "link",
            "set",
            "dev",
            "svmb1234",
            "alias",
            "smolvm-bridge:vm001",
        ]

    def test_prepare_refuses_existing_foreign_interface(self) -> None:
        from smolvm.exceptions import NetworkError
        from smolvm.host.network import BridgeInspection, NetworkManager

        nm = NetworkManager()
        with (
            patch.object(
                nm,
                "inspect_bridge",
                return_value=BridgeInspection("br10", True),
            ),
            patch.object(nm, "create_tap", return_value=False) as create_tap,
            patch.object(
                nm,
                "_get_link_info",
                return_value=self._tap_info(alias="someone-else"),
            ),
            patch.object(nm, "_set_tap_master") as set_master,
            patch.object(nm, "cleanup_tap") as cleanup_tap,
            pytest.raises(NetworkError, match="does not belong"),
        ):
            nm.prepare_bridged_tap("svmb1234", "br10", "vm001")

        create_tap.assert_not_called()
        set_master.assert_not_called()
        cleanup_tap.assert_not_called()

    def test_prepare_reuses_existing_owned_tap_without_recreating(self) -> None:
        from smolvm.host.network import BridgeInspection, NetworkManager

        nm = NetworkManager()
        owned = self._tap_info(alias="smolvm-bridge:vm001", master="br10")
        with (
            patch.object(
                nm,
                "inspect_bridge",
                return_value=BridgeInspection("br10", True),
            ),
            patch.object(nm, "create_tap") as create_tap,
            patch.object(nm, "_get_link_info", return_value=owned),
            patch.object(nm, "_set_tap_master") as set_master,
            patch("smolvm.host.network.run_command"),
        ):
            nm.prepare_bridged_tap("svmb1234", "br10", "vm001", user="alice")

        create_tap.assert_not_called()
        set_master.assert_called_once_with("svmb1234", "br10")

    def test_cleanup_retries_until_owned_tap_disappears(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        owned = self._tap_info(alias="smolvm-bridge:vm001")
        with (
            patch.object(
                nm,
                "_get_link_info",
                side_effect=[owned, owned, owned, owned, owned, None],
            ),
            patch.object(nm, "cleanup_tap") as cleanup_tap,
            patch("smolvm.host.network.time.sleep") as sleep,
        ):
            nm.cleanup_bridged_tap("svmb1234", "vm001")

        assert cleanup_tap.call_count == 2
        sleep.assert_called_once()

    def test_cleanup_refuses_foreign_interface(self) -> None:
        from smolvm.exceptions import NetworkError
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        with (
            patch.object(
                nm,
                "_get_link_info",
                return_value=self._tap_info(alias="someone-else"),
            ),
            patch.object(nm, "cleanup_tap") as cleanup_tap,
            pytest.raises(NetworkError, match="does not belong"),
        ):
            nm.cleanup_bridged_tap("svmb1234", "vm001")

        cleanup_tap.assert_not_called()


class TestBridgeMacGeneration:
    """Tests for bridge MAC address generation."""

    def test_generate_bridge_mac_format(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        mac = nm.generate_bridge_mac()
        parts = mac.split(":")
        assert len(parts) == 6
        # Locally administered unicast prefix.
        assert parts[0].upper() == "02"

    def test_generate_bridge_mac_unique(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        macs = {nm.generate_bridge_mac() for _ in range(100)}
        assert len(macs) == 100

    def test_reserved_tap_suffix_maps_to_collision_free_mac(self) -> None:
        from smolvm.host.network import NetworkManager

        nm = NetworkManager()
        assert nm.generate_bridge_mac("svmb01020304") == "02:53:01:02:03:04"
        assert nm.generate_bridge_mac("svmb01020305") == "02:53:01:02:03:05"


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

    def test_reserve_tap_name_rejects_changed_attachment(self, tmp_path: Path) -> None:
        from smolvm.exceptions import NetworkError
        from smolvm.storage._sqlite import SQLiteStateManager

        manager = SQLiteStateManager(tmp_path / "test.db")
        config = _make_vm_config(tmp_path, vm_id="test-vm-mismatch")
        manager.create_vm(config)
        manager.reserve_tap_name(
            "test-vm-mismatch",
            mode="bridge",
            bridge_name="br10",
            requested_tap="svmb1111",
        )

        with pytest.raises(NetworkError, match="already reserves"):
            manager.reserve_tap_name(
                "test-vm-mismatch",
                mode="bridge",
                bridge_name="br20",
                requested_tap="svmb2222",
            )

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


class TestBridgeLifecycle:
    """Tests for bridge-specific lifecycle invariants."""

    @staticmethod
    def _manager(tmp_path: Path):
        from smolvm.host.network import BridgeInspection
        from smolvm.vm import SmolVMManager

        manager = SmolVMManager(
            data_dir=tmp_path / "data",
            socket_dir=tmp_path / "sockets",
            backend="qemu",
        )
        network = MagicMock()
        network.inspect_bridge.return_value = BridgeInspection("br10", True)
        network.generate_bridge_mac.return_value = "02:00:00:00:00:01"
        manager.network = network
        return manager, network

    @staticmethod
    def _config(tmp_path: Path, *, comm_channel: str | None = None) -> VMConfig:
        return _make_vm_config(
            tmp_path,
            vm_id="bridge-lifecycle",
            comm_channel=comm_channel,
            network_attachment=NetworkAttachmentConfig(mode="bridge", bridge="br10"),
        ).model_copy(update={"backend": "qemu", "disk_mode": "shared"})

    def test_create_rejects_image_without_guest_network_support_before_mutation(
        self,
        tmp_path: Path,
    ) -> None:
        from smolvm.comm.select import ChannelResolution
        from smolvm.exceptions import SmolVMError

        manager, network = self._manager(tmp_path)
        config = self._config(tmp_path, comm_channel="vsock").model_copy(
            update={"guest_managed_networking": False}
        )
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(
                manager,
                "_resolve_control_channel_for_config",
                return_value=ChannelResolution(kind="vsock"),
            ),
            pytest.raises(SmolVMError, match="cannot configure bridged networking"),
        ):
            manager.create(config)

        network.prepare_bridged_tap.assert_not_called()

    def test_create_requires_resolved_vsock_before_network_mutation(self, tmp_path: Path) -> None:
        from smolvm.comm.select import ChannelResolution
        from smolvm.exceptions import SmolVMError

        manager, network = self._manager(tmp_path)
        config = self._config(tmp_path)
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(
                manager,
                "_resolve_control_channel_for_config",
                return_value=ChannelResolution(kind="ssh"),
            ),
            pytest.raises(SmolVMError, match="needs fast shell support"),
        ):
            manager.create(config)

        network.prepare_bridged_tap.assert_not_called()
        assert manager.state.get_tap_allocation(config.vm_id) is None

    def test_create_persists_network_before_preparing_owned_tap(self, tmp_path: Path) -> None:
        from smolvm.comm.select import ChannelResolution

        manager, network = self._manager(tmp_path)
        config = self._config(tmp_path, comm_channel="vsock")
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(
                manager,
                "_resolve_control_channel_for_config",
                return_value=ChannelResolution(kind="vsock"),
            ),
        ):
            info = manager.create(config)

        assert info.network is not None
        assert info.network.mode == "bridge"
        assert info.config.qemu_network == "tap"
        network.generate_bridge_mac.assert_called_once_with(info.network.tap_device)
        network.prepare_bridged_tap.assert_called_once_with(
            info.network.tap_device,
            "br10",
            config.vm_id,
            user=ANY,
        )

    def test_create_failure_cleans_reserved_owned_tap(self, tmp_path: Path) -> None:
        from smolvm.comm.select import ChannelResolution

        manager, network = self._manager(tmp_path)
        config = self._config(tmp_path, comm_channel="vsock")
        network.prepare_bridged_tap.side_effect = RuntimeError("attach failed")
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(
                manager,
                "_resolve_control_channel_for_config",
                return_value=ChannelResolution(kind="vsock"),
            ),
            pytest.raises(RuntimeError, match="attach failed"),
        ):
            manager.create(config)

        allocation = network.prepare_bridged_tap.call_args.args[0]
        network.cleanup_bridged_tap.assert_called_once_with(allocation, config.vm_id)

    def test_restore_network_reserves_and_repairs_bridge_tap_without_ip(
        self,
        tmp_path: Path,
    ) -> None:
        manager, network = self._manager(tmp_path)
        config = self._config(tmp_path, comm_channel="vsock")
        manager.state.create_vm(config)
        runtime_network = NetworkConfig(
            mode="bridge",
            bridge="br10",
            tap_device="svmbrestore",
            guest_mac="02:00:00:00:00:01",
        )

        from smolvm.comm.select import ChannelResolution

        with (
            patch("platform.system", return_value="Linux"),
            patch.object(
                manager,
                "_resolve_control_channel_for_config",
                return_value=ChannelResolution(kind="vsock"),
            ),
        ):
            manager._ensure_firecracker_network_for_restore(
                config.vm_id,
                runtime_network,
                vm_config=config,
            )

        assert manager.state.get_tap_allocation(config.vm_id) == (
            "svmbrestore",
            "bridge",
            "br10",
        )
        assert manager.state.get_ip_lease(config.vm_id) is None
        network.prepare_bridged_tap.assert_called_once_with(
            "svmbrestore",
            "br10",
            config.vm_id,
            user=ANY,
        )

    def test_start_rejects_inconsistent_persisted_bridge_before_network_mutation(
        self,
        tmp_path: Path,
    ) -> None:
        from smolvm.exceptions import SmolVMError

        manager, network = self._manager(tmp_path)
        config = self._config(tmp_path, comm_channel="vsock")
        manager.state.create_vm(config)
        manager.state.update_vm(
            config.vm_id,
            network=NetworkConfig(
                mode="bridge",
                bridge="br11",
                tap_device="svmbmismatch",
                guest_mac="02:00:00:00:00:01",
            ),
        )

        with pytest.raises(SmolVMError, match="inconsistent bridge settings"):
            manager.start(config.vm_id)

        network.prepare_bridged_tap.assert_not_called()

    def test_cleanup_foreign_replacement_releases_only_persisted_reservation(
        self,
        tmp_path: Path,
    ) -> None:
        from smolvm.exceptions import BridgeTapOwnershipError

        manager, network = self._manager(tmp_path)
        config = self._config(tmp_path, comm_channel="vsock")
        manager.state.create_vm(config)
        tap_name = manager.state.reserve_tap_name(
            config.vm_id,
            mode="bridge",
            bridge_name="br10",
        )
        manager.state.update_vm(
            config.vm_id,
            network=NetworkConfig(
                mode="bridge",
                bridge="br10",
                tap_device=tap_name,
                guest_mac="02:00:00:00:00:01",
            ),
        )
        network.cleanup_bridged_tap.side_effect = BridgeTapOwnershipError("foreign interface")

        manager._cleanup_resources(config.vm_id)

        network.cleanup_bridged_tap.assert_called_once_with(tap_name, config.vm_id)
        assert manager.state.get_tap_allocation(config.vm_id) is None

    def test_delete_retains_vm_and_reservation_when_owned_tap_cleanup_fails(
        self,
        tmp_path: Path,
    ) -> None:
        from smolvm.exceptions import NetworkError

        manager, network = self._manager(tmp_path)
        config = self._config(tmp_path, comm_channel="vsock")
        manager.state.create_vm(config)
        tap_name = manager.state.reserve_tap_name(
            config.vm_id,
            mode="bridge",
            bridge_name="br10",
        )
        manager.state.update_vm(
            config.vm_id,
            network=NetworkConfig(
                mode="bridge",
                bridge="br10",
                tap_device=tap_name,
                guest_mac="02:00:00:00:00:01",
            ),
        )
        network.cleanup_bridged_tap.side_effect = NetworkError("still busy")

        with pytest.raises(NetworkError, match="still busy"):
            manager.delete(config.vm_id)

        assert manager.state.get_vm(config.vm_id).vm_id == config.vm_id
        assert manager.state.get_tap_allocation(config.vm_id) == (
            tap_name,
            "bridge",
            "br10",
        )

    @pytest.mark.asyncio
    async def test_async_delete_retains_reservation_when_owned_tap_cleanup_fails(
        self,
        tmp_path: Path,
    ) -> None:
        from smolvm.exceptions import NetworkError

        manager, network = self._manager(tmp_path)
        config = self._config(tmp_path, comm_channel="vsock")
        manager.state.create_vm(config)
        tap_name = manager.state.reserve_tap_name(
            config.vm_id,
            mode="bridge",
            bridge_name="br10",
        )
        manager.state.update_vm(
            config.vm_id,
            network=NetworkConfig(
                mode="bridge",
                bridge="br10",
                tap_device=tap_name,
                guest_mac="02:00:00:00:00:01",
            ),
        )
        network.async_cleanup_bridged_tap.side_effect = NetworkError("still busy")

        with pytest.raises(NetworkError, match="still busy"):
            await manager.async_delete(config.vm_id)

        assert manager.state.get_tap_allocation(config.vm_id) == (
            tap_name,
            "bridge",
            "br10",
        )

    def test_manager_rejects_mount_added_after_model_validation(self, tmp_path: Path) -> None:
        from smolvm.comm.select import ChannelResolution
        from smolvm.exceptions import SmolVMError
        from smolvm.types import WorkspaceMount

        manager, network = self._manager(tmp_path)
        config = self._config(tmp_path, comm_channel="vsock")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = config.model_copy(
            update={"workspace_mounts": [WorkspaceMount(host_path=workspace)]}
        )
        with (
            patch("platform.system", return_value="Linux"),
            patch.object(
                manager,
                "_resolve_control_channel_for_config",
                return_value=ChannelResolution(kind="vsock"),
            ),
            pytest.raises(SmolVMError, match="cannot share host folders"),
        ):
            manager.create(config)

        network.prepare_bridged_tap.assert_not_called()


class TestBridgeUnsupportedOperations:
    """Bridge mode should reject host-IP-dependent operations immediately."""

    @staticmethod
    def _facade(tmp_path: Path):
        from smolvm.facade import SmolVM
        from smolvm.types import VMInfo, VMState

        config = _make_vm_config(
            tmp_path,
            vm_id="bridge-ops",
            comm_channel="vsock",
            network_attachment=NetworkAttachmentConfig(mode="bridge", bridge="br10"),
        ).model_copy(update={"backend": "qemu", "qemu_network": "tap"})
        network = NetworkConfig(
            mode="bridge",
            bridge="br10",
            tap_device="svmbops",
            guest_mac="02:00:00:00:00:02",
        )
        vm = object.__new__(SmolVM)
        vm._vm_id = config.vm_id
        vm._info = VMInfo(
            vm_id=config.vm_id,
            status=VMState.RUNNING,
            config=config,
            network=network,
        )
        vm._local_forwards = {}
        vm._sdk = MagicMock()
        vm._refresh_info = MagicMock()
        return vm

    def test_ssh_endpoint_recommends_shell(self, tmp_path: Path) -> None:
        from smolvm.exceptions import SmolVMError

        vm = self._facade(tmp_path)
        with pytest.raises(SmolVMError, match="sandbox shell"):
            vm._ssh_endpoints()

    def test_port_exposure_rejects_bridge_before_host_mutation(self, tmp_path: Path) -> None:
        from smolvm.exceptions import SmolVMError

        vm = self._facade(tmp_path)
        with pytest.raises(SmolVMError, match="already connected directly"):
            vm.expose_local(8080)

        vm._sdk.ensure_network_connectivity.assert_not_called()


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
