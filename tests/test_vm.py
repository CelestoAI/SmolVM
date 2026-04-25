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

"""Tests for SmolVM main SDK class."""

import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from smolvm.exceptions import (
    SmolVMError,
    VMAlreadyExistsError,
    VMNotFoundError,
)
from smolvm.types import VMConfig, VMState
from smolvm.vm import SmolVMManager


@pytest.fixture
def smol_vm(tmp_path: Path) -> SmolVMManager:
    """Create a SmolVM instance with temporary directories."""
    return SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="firecracker",
    )


@pytest.fixture
def sample_config(tmp_path: Path) -> VMConfig:
    """Create a sample VMConfig."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()

    return VMConfig(
        vm_id="vm001",
        vcpu_count=2,
        memory=512,
        kernel_path=kernel,
        rootfs_path=rootfs,
    )


class TestSmolVMCreate:
    """Tests for VM creation."""

    @patch("smolvm.vm.NetworkManager")
    def test_create_vm_allocates_resources(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Test that create allocates IP and TAP."""
        # Setup mock
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        vm_info = smol_vm.create(sample_config)

        assert vm_info.vm_id == "vm001"
        assert vm_info.status == VMState.CREATED
        assert vm_info.network is not None
        assert vm_info.network.guest_ip.startswith("172.16.0.")

        # Verify network setup was called
        mock_network.create_tap.assert_called_once()
        mock_network.configure_tap.assert_called_once()
        mock_network.setup_nat.assert_called_once()
        mock_network.setup_ssh_port_forward.assert_called_once()

    @patch("smolvm.vm.NetworkManager")
    def test_create_duplicate_raises(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Test that creating duplicate VM raises error."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        smol_vm.create(sample_config)

        with pytest.raises(VMAlreadyExistsError):
            smol_vm.create(sample_config)

    @patch("smolvm.vm.NetworkManager")
    def test_create_rollback_on_network_failure(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Test that resources are cleaned up on failure."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.create_tap.side_effect = Exception("Network error")
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        with pytest.raises(Exception, match="Network error"):
            smol_vm.create(sample_config)

        # VM should not exist after rollback
        with pytest.raises(VMNotFoundError):
            smol_vm.get("vm001")

    @patch("smolvm.vm.NetworkManager")
    def test_create_libkrun_uses_usernet_networking(
        self,
        mock_network_class: MagicMock,
        tmp_path: Path,
        sample_config: VMConfig,
    ) -> None:
        """libkrun backend should reuse usernet-style networking without TAP/NAT setup."""
        smol_vm = SmolVMManager(
            data_dir=tmp_path / "data-libkrun",
            socket_dir=tmp_path / "sockets-libkrun",
            backend="libkrun",
        )
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        vm_info = smol_vm.create(sample_config.model_copy(update={"vm_id": "vm-libkrun"}))

        assert vm_info.network is not None
        assert vm_info.network.tap_device == "usernet"
        mock_network.create_tap.assert_not_called()
        mock_network.setup_nat.assert_not_called()
        mock_network.setup_ssh_port_forward.assert_not_called()

    def test_check_prerequisites_libkrun_only_checks_krunvm_and_ssh(self, tmp_path: Path) -> None:
        """libkrun prerequisite checks should not require qemu/qemu-img."""
        smol_vm = SmolVMManager(
            data_dir=tmp_path / "data-libkrun-preflight",
            socket_dir=tmp_path / "sockets-libkrun-preflight",
            backend="libkrun",
        )

        with (
            patch.object(smol_vm, "_find_krunvm_binary", return_value=Path("/usr/bin/krunvm")),
            patch("smolvm.vm.which", return_value=Path("/usr/bin/ssh")),
            patch.object(smol_vm, "_find_qemu_binary", return_value=None),
            patch.object(smol_vm, "_find_qemu_img_binary", return_value=None),
        ):
            assert smol_vm.check_prerequisites() == []


class TestSmolVMDiskLifecycle:
    """Tests for per-VM disk materialization and cleanup."""

    @patch("smolvm.vm.NetworkManager")
    def test_create_materializes_isolated_disk(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Isolated mode should clone rootfs into data_dir/disks per VM."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        vm_info = smol_vm.create(sample_config)

        expected_disk = smol_vm.data_dir / "disks" / "vm001.ext4"
        assert vm_info.config.rootfs_path == expected_disk
        assert expected_disk.exists()

    @patch("smolvm.vm.NetworkManager")
    def test_shared_disk_mode_uses_original_rootfs(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Shared disk mode should use the caller-provided rootfs path directly."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        config = sample_config.model_copy(update={"disk_mode": "shared"})
        vm_info = smol_vm.create(config)

        assert vm_info.config.rootfs_path == sample_config.rootfs_path
        assert not (smol_vm.data_dir / "disks" / "vm001.ext4").exists()

    @patch("smolvm.vm.NetworkManager")
    def test_delete_removes_isolated_disk_by_default(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Deleting a VM removes its isolated disk unless retention is enabled."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        smol_vm.create(sample_config)
        disk_path = smol_vm.data_dir / "disks" / "vm001.ext4"
        assert disk_path.exists()

        smol_vm.delete("vm001")

        assert not disk_path.exists()

    @patch("smolvm.vm.NetworkManager")
    def test_delete_retains_isolated_disk_when_enabled(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """retain_disk_on_delete preserves isolated disk for later reuse."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        retained_config = sample_config.model_copy(update={"retain_disk_on_delete": True})
        smol_vm.create(retained_config)
        disk_path = smol_vm.data_dir / "disks" / "vm001.ext4"
        assert disk_path.exists()

        smol_vm.delete("vm001")

        assert disk_path.exists()

    @patch("smolvm.vm.NetworkManager")
    def test_create_reuses_retained_disk_for_same_vm_id(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """A retained isolated disk should be reused for a recreated VM ID."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        retained_config = sample_config.model_copy(update={"retain_disk_on_delete": True})
        smol_vm.create(retained_config)
        disk_path = smol_vm.data_dir / "disks" / "vm001.ext4"
        original_mtime = disk_path.stat().st_mtime_ns

        smol_vm.delete("vm001")
        assert disk_path.exists()

        recreated = smol_vm.create(retained_config)
        assert recreated.config.rootfs_path == disk_path
        assert disk_path.stat().st_mtime_ns == original_mtime


class TestSmolVMGet:
    """Tests for getting VM info."""

    @patch("smolvm.vm.NetworkManager")
    def test_get_existing_vm(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Test getting an existing VM."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        smol_vm.create(sample_config)

        vm_info = smol_vm.get("vm001")

        assert vm_info.vm_id == "vm001"

    def test_get_nonexistent_raises(self, smol_vm: SmolVMManager) -> None:
        """Test that getting nonexistent VM raises error."""
        with pytest.raises(VMNotFoundError):
            smol_vm.get("nonexistent")


class TestSmolVMList:
    """Tests for listing VMs."""

    @patch("smolvm.vm.NetworkManager")
    def test_list_empty(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
    ) -> None:
        """Test listing when no VMs exist."""
        vms = smol_vm.list_vms()
        assert vms == []

    @patch("smolvm.vm.NetworkManager")
    def test_list_multiple(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        tmp_path: Path,
    ) -> None:
        """Test listing multiple VMs."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        for i in range(3):
            kernel = tmp_path / f"vmlinux{i}"
            rootfs = tmp_path / f"rootfs{i}.ext4"
            kernel.touch()
            rootfs.touch()

            config = VMConfig(
                vm_id=f"vm{i:03d}",
                kernel_path=kernel,
                rootfs_path=rootfs,
            )
            smol_vm.create(config)

        vms = smol_vm.list_vms()
        assert len(vms) == 3


class TestSmolVMDelete:
    """Tests for VM deletion."""

    @patch("smolvm.vm.NetworkManager")
    def test_delete_vm(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Test deleting a VM."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        smol_vm.create(sample_config)
        smol_vm.delete("vm001")

        with pytest.raises(VMNotFoundError):
            smol_vm.get("vm001")

    def test_delete_nonexistent_raises(self, smol_vm: SmolVMManager) -> None:
        """Test that deleting nonexistent VM raises error."""
        with pytest.raises(VMNotFoundError):
            smol_vm.delete("nonexistent")

    @patch("smolvm.vm.NetworkManager")
    def test_delete_cleans_local_forward_rules(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Delete should clean local-forward nftables rules by vm_id."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        smol_vm.create(sample_config)
        smol_vm.delete("vm001")

        mock_network.cleanup_all_local_port_forwards.assert_called_once_with("vm001")
        mock_network.remove_egress_rules.assert_called_once_with("tap2")


class TestIPBasedTAPNaming:
    """Tests for IP-allocation-based TAP naming."""

    @patch("smolvm.vm.NetworkManager")
    def test_create_uses_ip_for_tap_name(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Test that TAP name is derived from the IP last octet."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:02"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        vm_info = smol_vm.create(sample_config)

        import os

        expected_user = os.environ.get("USER", "root")

        # First IP allocated is 172.16.0.2, so TAP should be tap2
        assert vm_info.network is not None
        assert vm_info.network.tap_device == "tap2"
        assert vm_info.network.guest_ip == "172.16.0.2"
        assert vm_info.network.ssh_host_port == 2200
        mock_network.create_tap.assert_called_once_with("tap2", expected_user)

    @patch("smolvm.vm.NetworkManager")
    def test_sequential_vms_get_unique_taps(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        tmp_path: Path,
    ) -> None:
        """Test that sequential VMs get unique TAP names based on IPs."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:02"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        tap_names = []
        for i in range(3):
            kernel = tmp_path / f"vmlinux{i}"
            rootfs = tmp_path / f"rootfs{i}.ext4"
            kernel.touch()
            rootfs.touch()

            config = VMConfig(
                vm_id=f"vm{i:03d}",
                kernel_path=kernel,
                rootfs_path=rootfs,
            )
            vm_info = smol_vm.create(config)
            tap_names.append(vm_info.network.tap_device)

        # IPs 172.16.0.2, .3, .4 → tap2, tap3, tap4
        assert tap_names == ["tap2", "tap3", "tap4"]


class TestSmolVMContextManager:
    """Tests for context manager support."""

    def test_context_manager(self, tmp_path: Path) -> None:
        """Test that SmolVM can be used with 'with' statement."""
        with SmolVMManager(
            data_dir=tmp_path / "data",
            socket_dir=tmp_path / "sockets",
            backend="firecracker",
        ) as sdk:
            assert sdk is not None
            assert not sdk._closed

        assert sdk._closed

    def test_close_is_idempotent(self, smol_vm: SmolVMManager) -> None:
        """Test that close() can be called multiple times safely."""
        smol_vm.close()
        smol_vm.close()  # Should not raise
        assert smol_vm._closed


class TestSmolVMFromId:
    """Tests for from_id class method."""

    @patch("smolvm.vm.NetworkManager")
    def test_from_id_existing(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
        tmp_path: Path,
    ) -> None:
        """Test from_id works for an existing VM."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:02"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        smol_vm.create(sample_config)

        # from_id should succeed and return a new SDK instance
        sdk2 = SmolVMManager.from_id(
            "vm001",
            data_dir=tmp_path / "data",
            socket_dir=tmp_path / "sockets",
        )
        vm = sdk2.get("vm001")
        assert vm.vm_id == "vm001"

    def test_from_id_nonexistent(self, tmp_path: Path) -> None:
        """Test from_id raises for nonexistent VM."""
        with pytest.raises(VMNotFoundError):
            SmolVMManager.from_id(
                "nonexistent",
                data_dir=tmp_path / "data",
                socket_dir=tmp_path / "sockets",
            )


class TestSmolVMBootArgsAndSSHCommands:
    """Tests for boot-arg injection and SSH helper commands."""

    @patch("smolvm.runtime.firecracker.FirecrackerClient")
    @patch.object(SmolVMManager, "_start_firecracker")
    @patch("smolvm.vm.NetworkManager")
    def test_start_injects_ip_boot_arg_when_missing(
        self,
        mock_network_class: MagicMock,
        mock_start_fc: MagicMock,
        mock_client_cls: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Test start() auto-injects ip= boot arg if not present."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:02"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_start_fc.return_value = mock_process

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        smol_vm.create(sample_config)
        smol_vm.start("vm001")

        boot_args = mock_client.set_boot_source.call_args[0][1]
        assert "ip=172.16.0.2::172.16.0.1:255.255.255.0::eth0:off" in boot_args

    @patch("smolvm.runtime.firecracker.FirecrackerClient")
    @patch.object(SmolVMManager, "_start_firecracker")
    @patch("smolvm.vm.NetworkManager")
    def test_start_preserves_existing_ip_boot_arg(
        self,
        mock_network_class: MagicMock,
        mock_start_fc: MagicMock,
        mock_client_cls: MagicMock,
        smol_vm: SmolVMManager,
        tmp_path: Path,
    ) -> None:
        """Test start() does not override caller-provided ip= boot args."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:02"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_start_fc.return_value = mock_process

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        kernel = tmp_path / "vmlinux-ip"
        rootfs = tmp_path / "rootfs-ip.ext4"
        kernel.touch()
        rootfs.touch()
        config = VMConfig(
            vm_id="vm002",
            kernel_path=kernel,
            rootfs_path=rootfs,
            boot_args="console=ttyS0 ip=10.0.0.2::10.0.0.1:255.255.255.0::eth0:off",
        )

        smol_vm.create(config)
        smol_vm.start("vm002")

        boot_args = mock_client.set_boot_source.call_args[0][1]
        assert boot_args == config.boot_args

    @patch("smolvm.runtime.firecracker.FirecrackerClient")
    @patch.object(SmolVMManager, "_start_firecracker")
    @patch("smolvm.vm.NetworkManager")
    def test_start_attaches_extra_drives(
        self,
        mock_network_class: MagicMock,
        mock_start_fc: MagicMock,
        mock_client_cls: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
        tmp_path: Path,
    ) -> None:
        """Test start() attaches configured extra drives via Firecracker drives API."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:02"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_start_fc.return_value = mock_process

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        data_drive = tmp_path / "data.ext4"
        data_drive.touch()
        config = sample_config.model_copy(update={"extra_drives": [data_drive]})

        smol_vm.create(config)
        smol_vm.start("vm001")

        assert mock_client.add_drive.call_count == 2
        assert mock_client.add_drive.call_args_list[1] == call(
            "data_drive",
            data_drive,
            is_root_device=False,
            is_read_only=False,
        )

    @patch("smolvm.vm.NetworkManager")
    def test_get_ssh_commands_returns_private_and_forwarded(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Test SSH helper command output includes forwarded host port."""
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:02"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        smol_vm.create(sample_config)
        cmds = smol_vm.get_ssh_commands("vm001", public_host="203.0.113.10")

        assert cmds["private_ip"] == "ssh root@172.16.0.2"
        assert cmds["localhost_port"] == "ssh -p 2200 root@127.0.0.1"
        assert cmds["public"] == "ssh -p 2200 root@203.0.113.10"


class TestDataDirResolution:
    """Tests for automatic data-directory resolution."""

    def test_explicit_data_dir_wins_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit constructor arg should take precedence over environment override."""
        explicit_dir = tmp_path / "explicit"
        env_dir = tmp_path / "env"
        monkeypatch.setenv("SMOLVM_DATA_DIR", str(env_dir))

        sdk = SmolVMManager(
            data_dir=explicit_dir,
            socket_dir=tmp_path / "sockets",
            backend="firecracker",
        )
        try:
            assert sdk.data_dir == explicit_dir
            assert sdk.data_dir.exists()
        finally:
            sdk.close()

    def test_uses_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """SMOLVM_DATA_DIR should be used when data_dir is not provided."""
        env_dir = tmp_path / "state-from-env"
        monkeypatch.setenv("SMOLVM_DATA_DIR", str(env_dir))
        monkeypatch.delenv("SUDO_USER", raising=False)

        sdk = SmolVMManager(socket_dir=tmp_path / "sockets", backend="firecracker")
        try:
            assert sdk.data_dir == env_dir
            assert (env_dir / "smolvm.db").exists()
        finally:
            sdk.close()

    def test_uses_xdg_state_home_for_non_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-root default should prefer $XDG_STATE_HOME/smolvm."""
        xdg_state_home = tmp_path / "xdg-state"
        monkeypatch.delenv("SMOLVM_DATA_DIR", raising=False)
        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state_home))

        with patch("smolvm.vm.os.geteuid", return_value=1000):
            sdk = SmolVMManager(socket_dir=tmp_path / "sockets", backend="firecracker")

        try:
            assert sdk.data_dir == xdg_state_home / "smolvm"
            assert (sdk.data_dir / "smolvm.db").exists()
        finally:
            sdk.close()

    def test_sudo_user_prefers_real_user_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under sudo, default should use the sudo user's home state path."""
        sudo_home = tmp_path / "sudo-home"
        fake_passwd = SimpleNamespace(
            pw_dir=str(sudo_home),
            pw_uid=1234,
            pw_gid=1234,
        )

        monkeypatch.delenv("SMOLVM_DATA_DIR", raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setenv("SUDO_USER", "alice")

        with (
            patch("smolvm.vm.os.geteuid", return_value=0),
            patch("smolvm.vm.pwd.getpwnam", return_value=fake_passwd),
            patch("smolvm.vm.os.chown"),
        ):
            sdk = SmolVMManager(socket_dir=tmp_path / "sockets", backend="firecracker")

        try:
            assert sdk.data_dir == sudo_home / ".local" / "state" / "smolvm"
            assert (sdk.data_dir / "smolvm.db").exists()
        finally:
            sdk.close()


class TestFirecrackerLaunchAndSocketCleanup:
    """Tests for Firecracker launch mode and socket cleanup behavior."""

    def test_start_firecracker_runs_without_sudo(
        self, smol_vm: SmolVMManager, tmp_path: Path
    ) -> None:
        """Firecracker should run as current user; no sudo prefix in launch command."""
        socket_path = tmp_path / "sockets" / "fc-vm.sock"
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        log_path = tmp_path / "data" / "vm.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        mock_process = MagicMock()
        mock_process.pid = 12345

        with (
            patch.object(
                smol_vm.host,
                "find_firecracker",
                return_value=Path("/usr/bin/firecracker"),
            ),
            patch("smolvm.vm.subprocess.Popen", return_value=mock_process) as mock_popen,
        ):
            smol_vm._start_firecracker(socket_path, log_path)

        cmd = mock_popen.call_args[0][0]
        assert cmd == ["/usr/bin/firecracker", "--api-sock", str(socket_path)]
        kwargs = mock_popen.call_args.kwargs
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["start_new_session"] is True

    @patch("smolvm.vm.subprocess.run")
    @patch("pathlib.Path.unlink")
    def test_unlink_socket_permission_error_uses_sudo_fallback(
        self,
        mock_unlink: MagicMock,
        mock_run: MagicMock,
        smol_vm: SmolVMManager,
    ) -> None:
        """Permission errors should trigger sudo rm fallback for stale root sockets."""
        mock_unlink.side_effect = PermissionError
        mock_run.return_value = SimpleNamespace(returncode=0, stderr="")

        smol_vm._unlink_socket(Path("/tmp/fc-test.sock"))

        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == ["sudo", "-n", "rm", "-f", "/tmp/fc-test.sock"]

    @patch("smolvm.vm.subprocess.run")
    @patch("pathlib.Path.unlink")
    def test_unlink_socket_permission_error_reports_actionable_error(
        self,
        mock_unlink: MagicMock,
        mock_run: MagicMock,
        smol_vm: SmolVMManager,
    ) -> None:
        """If sudo fallback fails, raise a clear error with manual remediation."""
        mock_unlink.side_effect = PermissionError
        mock_run.return_value = SimpleNamespace(returncode=1, stderr="sudo: a password is required")

        with pytest.raises(SmolVMError, match="sudo rm -f /tmp/fc-test.sock"):
            smol_vm._unlink_socket(Path("/tmp/fc-test.sock"))


class TestProcessLifecycle:
    """Tests for process tracking, killing, and zombie reaping (issue #189)."""

    def test_is_process_running_reaps_zombie_via_handle(self, smol_vm: SmolVMManager) -> None:
        """A child that exited naturally must not be mistaken for a live process."""
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        smol_vm._process_handles[process.pid] = process

        deadline = time.time() + 5.0
        while process.poll() is None and time.time() < deadline:
            time.sleep(0.01)
        assert process.returncode is not None, "child failed to exit in time"

        # Without zombie reaping, os.kill(pid, 0) succeeds against the corpse and
        # _is_process_running returns True. The handle-based poll must catch it.
        assert smol_vm._is_process_running(process.pid) is False
        assert process.pid not in smol_vm._process_handles

    def test_kill_process_reaps_handle_so_followup_check_returns_false(
        self, smol_vm: SmolVMManager
    ) -> None:
        """SIGKILL via _kill_process should leave _is_process_running returning False."""
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        smol_vm._process_handles[process.pid] = process
        try:
            smol_vm._kill_process(process.pid)
            assert smol_vm._is_process_running(process.pid) is False
            assert process.pid not in smol_vm._process_handles
        finally:
            if process.poll() is None:
                process.kill()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2.0)

    def test_wait_for_process_uses_handle_and_drops_pid(self, smol_vm: SmolVMManager) -> None:
        """_wait_for_process should block via Popen.wait() and drop the handle."""
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.1)"])
        smol_vm._process_handles[process.pid] = process

        smol_vm._wait_for_process(process.pid, timeout=5.0)

        assert process.poll() is not None
        assert process.pid not in smol_vm._process_handles
