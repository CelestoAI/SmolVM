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
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from smolvm.comm.select import ChannelResolution
from smolvm.exceptions import (
    SmolVMError,
    VMAlreadyExistsError,
    VMNotFoundError,
)
from smolvm.types import InternetSettings, VMConfig, VMInfo, VMState, WorkspaceMount
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


def _attach_mock_network(manager: SmolVMManager) -> MagicMock:
    """Attach a network mock that supports sync and async create paths."""
    mock_network = MagicMock()
    mock_network.host_ip = "172.16.0.1"
    mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
    mock_network.async_create_tap = AsyncMock()
    mock_network.async_configure_tap = AsyncMock()
    mock_network.async_add_route = AsyncMock()
    mock_network.async_setup_nat = AsyncMock()
    mock_network.async_apply_egress_allowlist = AsyncMock()
    mock_network.async_setup_ssh_port_forward = AsyncMock()
    manager.network = mock_network
    return mock_network


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
    def test_create_rollback_preserves_preexisting_managed_disk(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Create rollback must not delete a disk retained from an earlier VM."""
        retained_disk = smol_vm.data_dir / "disks" / "vm001.ext4"
        retained_disk.parent.mkdir(parents=True, exist_ok=True)
        retained_disk.write_text("retained")
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.create_tap.side_effect = Exception("Network error")
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        with pytest.raises(Exception, match="Network error"):
            smol_vm.create(sample_config)

        assert retained_disk.read_text() == "retained"
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

    def test_check_prerequisites_libkrun_only_checks_library_and_ssh(self, tmp_path: Path) -> None:
        """libkrun prerequisite checks should not require qemu/qemu-img."""
        smol_vm = SmolVMManager(
            data_dir=tmp_path / "data-libkrun-preflight",
            socket_dir=tmp_path / "sockets-libkrun-preflight",
            backend="libkrun",
        )

        with (
            patch.object(smol_vm, "_find_libkrun_library", return_value=True),
            patch("smolvm.vm.which", return_value=Path("/usr/bin/ssh")),
            patch.object(smol_vm, "_find_qemu_binary", return_value=None),
            patch.object(smol_vm, "_find_qemu_img_binary", return_value=None),
        ):
            assert smol_vm.check_prerequisites() == []

    @patch("smolvm.comm.select.platform.system", return_value="Linux")
    def test_create_firecracker_explicit_vsock_skips_ssh_forward(
        self,
        _mock_system: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Explicit Firecracker vsock without SSH-backed startup should not add DNAT."""
        mock_network = _attach_mock_network(smol_vm)
        config = sample_config.model_copy(
            update={"vm_id": "vm-vsock", "backend": "firecracker", "comm_channel": "vsock"}
        )

        vm_info = smol_vm.create(config)

        assert vm_info.network is not None
        assert vm_info.network.ssh_host_port is None
        assert vm_info.config.vsock is not None
        mock_network.create_tap.assert_called_once()
        mock_network.configure_tap.assert_called_once()
        mock_network.add_route.assert_not_called()
        mock_network.setup_nat.assert_not_called()
        mock_network.setup_ssh_port_forward.assert_not_called()

    @patch("smolvm.comm.select.platform.system", return_value="Linux")
    def test_create_firecracker_explicit_vsock_lazy_network_can_be_activated(
        self,
        _mock_system: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Deferred Firecracker network connectivity is installed before network use."""
        mock_network = _attach_mock_network(smol_vm)
        config = sample_config.model_copy(
            update={"vm_id": "vm-vsock-lazy", "backend": "firecracker", "comm_channel": "vsock"}
        )
        vm_info = smol_vm.create(config)

        mock_network.add_route.assert_not_called()
        mock_network.setup_nat.assert_not_called()

        smol_vm.ensure_network_connectivity(vm_info)

        mock_network.add_route.assert_called_once_with(
            vm_info.network.guest_ip,
            vm_info.network.tap_device,
        )
        mock_network.setup_nat.assert_called_once_with(vm_info.network.tap_device)
        mock_network.setup_ssh_port_forward.assert_not_called()

    @patch("smolvm.comm.select.platform.system", return_value="Linux")
    def test_create_firecracker_auto_keeps_ssh_forward_for_fallback(
        self,
        _mock_system: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Auto channel keeps SSH forwarding so vsock probe fallback still works."""
        mock_network = _attach_mock_network(smol_vm)
        config = sample_config.model_copy(update={"vm_id": "vm-auto", "backend": "firecracker"})

        vm_info = smol_vm.create(config)

        assert vm_info.network is not None
        assert vm_info.network.ssh_host_port is not None
        mock_network.add_route.assert_called_once()
        mock_network.setup_nat.assert_called_once()
        mock_network.setup_ssh_port_forward.assert_called_once()

    @patch("smolvm.comm.select.platform.system", return_value="Linux")
    def test_create_firecracker_explicit_ssh_keeps_ssh_forward(
        self,
        _mock_system: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Explicit SSH must reserve and expose a host SSH port."""
        mock_network = _attach_mock_network(smol_vm)
        config = sample_config.model_copy(
            update={"vm_id": "vm-ssh", "backend": "firecracker", "comm_channel": "ssh"}
        )

        vm_info = smol_vm.create(config)

        assert vm_info.network is not None
        assert vm_info.network.ssh_host_port is not None
        mock_network.add_route.assert_called_once()
        mock_network.setup_nat.assert_called_once()
        mock_network.setup_ssh_port_forward.assert_called_once()

    @patch("smolvm.comm.select.platform.system", return_value="Linux")
    def test_create_firecracker_explicit_vsock_with_env_keeps_ssh_forward(
        self,
        _mock_system: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Env injection is still SSH-backed at startup, so keep forwarding."""
        mock_network = _attach_mock_network(smol_vm)
        config = sample_config.model_copy(
            update={
                "vm_id": "vm-vsock-env",
                "backend": "firecracker",
                "comm_channel": "vsock",
                "env_vars": {"SMOLVM_TEST": "1"},
            }
        )

        vm_info = smol_vm.create(config)

        assert vm_info.network is not None
        assert vm_info.network.ssh_host_port is not None
        mock_network.add_route.assert_called_once()
        mock_network.setup_nat.assert_called_once()
        mock_network.setup_ssh_port_forward.assert_called_once()

    @patch("smolvm.comm.select.platform.system", return_value="Linux")
    @patch("smolvm.vm.resolve_domains_to_ips", return_value=["93.184.216.34"])
    def test_create_firecracker_explicit_vsock_with_allowlist_keeps_tap_connectivity(
        self,
        _mock_resolve: MagicMock,
        _mock_system: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Network policy needs route/NAT before boot even with explicit vsock."""
        mock_network = _attach_mock_network(smol_vm)
        config = sample_config.model_copy(
            update={
                "vm_id": "vm-vsock-policy",
                "backend": "firecracker",
                "comm_channel": "vsock",
                "internet_settings": InternetSettings(allowed_domains=["example.com"]),
            }
        )

        vm_info = smol_vm.create(config)

        assert vm_info.network is not None
        assert vm_info.network.ssh_host_port is None
        mock_network.add_route.assert_called_once()
        mock_network.setup_nat.assert_called_once()
        mock_network.apply_egress_allowlist.assert_called_once_with(
            vm_info.network.tap_device,
            ["93.184.216.34"],
        )
        mock_network.setup_ssh_port_forward.assert_not_called()

    @patch("smolvm.comm.select.host_supports_vsock", return_value=True)
    def test_create_qemu_slirp_explicit_vsock_keeps_ssh_hostfwd(
        self,
        _mock_host_vsock: MagicMock,
        tmp_path: Path,
        sample_config: VMConfig,
    ) -> None:
        """QEMU slirp keeps hostfwd for terminal compatibility under explicit vsock."""
        smol_vm = SmolVMManager(
            data_dir=tmp_path / "data-qemu-vsock",
            socket_dir=tmp_path / "sockets-qemu-vsock",
            backend="qemu",
        )
        mock_network = _attach_mock_network(smol_vm)
        config = sample_config.model_copy(
            update={
                "vm_id": "vm-qemu-vsock",
                "backend": "qemu",
                "comm_channel": "vsock",
                "qemu_network": "slirp",
                "disk_mode": "shared",
                "rootfs_format": "raw-ext4",
            }
        )

        with patch.object(smol_vm, "_create_qemu_overlay_disk") as mock_overlay:
            vm_info = smol_vm.create(config)

        assert vm_info.network is not None
        assert vm_info.network.tap_device == "usernet"
        assert vm_info.network.ssh_host_port is not None
        mock_overlay.assert_not_called()
        mock_network.setup_ssh_port_forward.assert_not_called()

    def test_qemu_tap_workspace_policy_keeps_ssh_forward(
        self,
        tmp_path: Path,
        sample_config: VMConfig,
    ) -> None:
        """Workspace startup remains SSH-backed even when the control channel is vsock."""
        smol_vm = SmolVMManager(
            data_dir=tmp_path / "data-qemu-workspace",
            socket_dir=tmp_path / "sockets-qemu-workspace",
            backend="qemu",
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = sample_config.model_copy(
            update={
                "vm_id": "vm-qemu-workspace",
                "backend": "qemu",
                "comm_channel": "vsock",
                "qemu_network": "tap",
                "workspace_mounts": [WorkspaceMount(host_path=workspace)],
            }
        )

        assert smol_vm._should_reserve_ssh_forward(
            config,
            "qemu",
            resolution=ChannelResolution(kind="vsock", allow_fallback=False),
        )

    def test_firecracker_workspace_policy_keeps_ssh_forward(
        self,
        tmp_path: Path,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Workspace startup is SSH-backed by policy even before backend validation."""
        workspace = tmp_path / "workspace-firecracker"
        workspace.mkdir()
        config = sample_config.model_copy(
            update={
                "vm_id": "vm-fc-workspace",
                "backend": "firecracker",
                "comm_channel": "vsock",
                "workspace_mounts": [WorkspaceMount(host_path=workspace)],
            }
        )

        assert smol_vm._should_reserve_ssh_forward(
            config,
            "firecracker",
            resolution=ChannelResolution(kind="vsock", allow_fallback=False),
        )

    def test_explicit_vsock_error_uses_recovery_payload(
        self,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Explicit-vsock create errors should not expose selector internals."""
        config = sample_config.model_copy(
            update={"vm_id": "vm-vsock-bad", "backend": "libkrun", "comm_channel": "vsock"}
        )

        with pytest.raises(SmolVMError) as exc_info:
            smol_vm._resolve_control_channel_for_config(config, "libkrun")

        assert (
            str(exc_info.value)
            == "Cannot use vsock for sandbox 'vm-vsock-bad': this backend does not support "
            "vsock in this release; create it with SSH by running: "
            "smolvm create --name vm-vsock-bad --backend libkrun."
        )
        assert exc_info.value.details == {
            "vm_id": "vm-vsock-bad",
            "recovery_command": "smolvm create --name vm-vsock-bad --backend libkrun",
        }

    @pytest.mark.asyncio
    @patch("smolvm.comm.select.platform.system", return_value="Linux")
    async def test_async_create_firecracker_explicit_vsock_skips_ssh_forward(
        self,
        _mock_system: MagicMock,
        tmp_path: Path,
        sample_config: VMConfig,
    ) -> None:
        """Async create should mirror the Firecracker explicit-vsock no-forward policy."""
        smol_vm = SmolVMManager(
            data_dir=tmp_path / "data-async-vsock",
            socket_dir=tmp_path / "sockets-async-vsock",
            backend="firecracker",
        )
        mock_network = _attach_mock_network(smol_vm)
        config = sample_config.model_copy(
            update={"vm_id": "vm-async-vsock", "backend": "firecracker", "comm_channel": "vsock"}
        )

        vm_info = await smol_vm.async_create(config)

        assert vm_info.network is not None
        assert vm_info.network.ssh_host_port is None
        mock_network.async_add_route.assert_not_awaited()
        mock_network.async_setup_nat.assert_not_awaited()
        mock_network.async_setup_ssh_port_forward.assert_not_awaited()


class TestSmolVMDiskLifecycle:
    """Tests for per-VM disk materialization and cleanup."""

    @staticmethod
    def _write_sparse_file(path: Path) -> None:
        with path.open("wb") as file:
            file.write(b"start")
            file.seek(4 * 1024 * 1024)
            file.write(b"end")

    @staticmethod
    def _assert_sparse_copy(source: Path, target: Path) -> None:
        assert target.stat().st_size == source.stat().st_size
        with target.open("rb") as file:
            assert file.read(5) == b"start"
            file.seek(4 * 1024 * 1024)
            assert file.read(3) == b"end"
        assert target.stat().st_blocks * 512 < target.stat().st_size

    def test_copy_with_reflink_preserves_sparse_holes(self, tmp_path: Path) -> None:
        """Raw isolated-disk copies should not inflate sparse rootfs holes."""
        source = tmp_path / "source.ext4"
        target = tmp_path / "target.ext4"
        source.write_bytes(b"rootfs")

        with patch("smolvm.vm.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(returncode=0)
            SmolVMManager._copy_with_reflink(source, target)

        mock_run.assert_called_once_with(
            ["cp", "--reflink=auto", "--sparse=always", str(source), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_copy_with_reflink_fallback_preserves_sparse_holes(self, tmp_path: Path) -> None:
        """The non-GNU cp fallback should also avoid writing sparse zero ranges."""
        source = tmp_path / "source.ext4"
        target = tmp_path / "target.ext4"
        self._write_sparse_file(source)

        with (
            patch("smolvm.vm.subprocess.run", return_value=SimpleNamespace(returncode=1)),
            patch("smolvm.vm.shutil.copy2", side_effect=AssertionError("must stay sparse")),
        ):
            SmolVMManager._copy_with_reflink(source, target)

        self._assert_sparse_copy(source, target)

    @pytest.mark.asyncio
    async def test_async_copy_with_reflink_fallback_preserves_sparse_holes(
        self,
        tmp_path: Path,
    ) -> None:
        """Async disk materialization should use the same sparse fallback."""
        source = tmp_path / "source.ext4"
        target = tmp_path / "target.ext4"
        self._write_sparse_file(source)
        manager = SmolVMManager(data_dir=tmp_path / "data", socket_dir=tmp_path / "sockets")

        with (
            patch("smolvm.utils.async_run_command", side_effect=SmolVMError("cp failed")),
            patch("smolvm.vm.shutil.copy2", side_effect=AssertionError("must stay sparse")),
        ):
            await manager._async_copy_with_reflink(source, target)

        self._assert_sparse_copy(source, target)

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
    def test_duplicate_create_does_not_resize_existing_disk(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Duplicate create should fail before touching the existing managed disk."""
        sample_config.rootfs_path.write_bytes(b"\0" * (1024 * 1024))
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        first_config = sample_config.model_copy(update={"disk_size_mib": 1})
        smol_vm.create(first_config)
        expected_disk = smol_vm.data_dir / "disks" / "vm001.ext4"
        assert expected_disk.stat().st_size == 1024 * 1024

        second_config = sample_config.model_copy(update={"disk_size_mib": 3})
        with pytest.raises(VMAlreadyExistsError):
            smol_vm.create(second_config)

        assert expected_disk.stat().st_size == 1024 * 1024

    @patch("smolvm.vm.NetworkManager")
    def test_create_resizes_and_grows_raw_isolated_disk(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Resize/grow applies to the per-VM raw ext4 disk, not the base image."""
        sample_config.rootfs_path.write_bytes(b"\0" * (1024 * 1024))
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        def _copy(source: Path, target: Path) -> None:
            target.write_bytes(source.read_bytes())

        config = sample_config.model_copy(update={"disk_size_mib": 2, "grow_filesystem": True})
        with (
            patch.object(SmolVMManager, "_copy_with_reflink", side_effect=_copy),
            patch.object(smol_vm, "_grow_raw_ext4_filesystem") as mock_grow,
        ):
            vm_info = smol_vm.create(config)

        expected_disk = smol_vm.data_dir / "disks" / "vm001.ext4"
        assert sample_config.rootfs_path.stat().st_size == 1024 * 1024
        assert expected_disk.stat().st_size == 2 * 1024 * 1024
        assert vm_info.config.rootfs_path == expected_disk
        mock_grow.assert_called_once_with(expected_disk, "vm001")

    def test_create_persistence_failure_does_not_resize_retained_disk(
        self,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """A reused managed disk is not mutated until the VM row exists."""
        retained_disk = smol_vm.data_dir / "disks" / "vm001.ext4"
        retained_disk.parent.mkdir(parents=True, exist_ok=True)
        retained_disk.write_bytes(b"\0" * (1024 * 1024))
        config = sample_config.model_copy(update={"disk_size_mib": 3})

        with (
            patch.object(smol_vm.state, "create_vm", side_effect=SmolVMError("persist failed")),
            pytest.raises(SmolVMError, match="persist failed"),
        ):
            smol_vm.create(config)

        assert retained_disk.exists()
        assert retained_disk.stat().st_size == 1024 * 1024

    def test_failed_grow_restores_retained_managed_disk(
        self,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Failed resize/grow rolls an existing retained disk back."""
        retained_disk = smol_vm.data_dir / "disks" / "vm001.ext4"
        retained_disk.parent.mkdir(parents=True, exist_ok=True)
        retained_disk.write_bytes(b"\0" * (1024 * 1024))
        config = sample_config.model_copy(update={"disk_size_mib": 2, "grow_filesystem": True})

        with (
            patch.object(
                smol_vm,
                "_grow_raw_ext4_filesystem",
                side_effect=SmolVMError("grow failed"),
            ),
            pytest.raises(SmolVMError, match="grow failed"),
        ):
            smol_vm.create(config)

        assert retained_disk.exists()
        assert retained_disk.stat().st_size == 1024 * 1024
        with pytest.raises(VMNotFoundError):
            smol_vm.get("vm001")

    @patch("smolvm.vm.NetworkManager")
    def test_create_persistence_failure_removes_new_managed_disk(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """If persisting the VM row fails, the pre-created disk is removed."""
        sample_config.rootfs_path.write_bytes(b"\0" * (1024 * 1024))
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        def _copy(source: Path, target: Path) -> None:
            target.write_bytes(source.read_bytes())

        expected_disk = smol_vm.data_dir / "disks" / "vm001.ext4"
        with (
            patch.object(SmolVMManager, "_copy_with_reflink", side_effect=_copy),
            patch.object(smol_vm.state, "create_vm", side_effect=SmolVMError("persist failed")),
            pytest.raises(SmolVMError, match="persist failed"),
        ):
            smol_vm.create(sample_config)

        assert not expected_disk.exists()
        with pytest.raises(VMNotFoundError):
            smol_vm.get("vm001")

    def test_e2fsck_successful_repairs_do_not_fail_growth(
        self,
        smol_vm: SmolVMManager,
        tmp_path: Path,
    ) -> None:
        """e2fsck may return 1 or 3 after repairs; resize2fs should still run."""
        disk = tmp_path / "rootfs.ext4"
        disk.touch()
        calls: list[list[str]] = []

        def _fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            del kwargs
            calls.append(command)
            return subprocess.CompletedProcess(command, 3 if "e2fsck" in command[0] else 0)

        with (
            patch("smolvm.vm.which", side_effect=Path),
            patch("smolvm.vm.subprocess.run", side_effect=_fake_run),
        ):
            smol_vm._grow_raw_ext4_filesystem(disk, "vm001")

        assert calls == [
            ["e2fsck", "-fy", str(disk)],
            ["resize2fs", str(disk)],
        ]

    @patch("smolvm.vm.NetworkManager")
    def test_failed_grow_removes_new_managed_disk(
        self,
        mock_network_class: MagicMock,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """If resize/grow fails before persistence, the partial disk is removed."""
        sample_config.rootfs_path.write_bytes(b"\0" * (1024 * 1024))
        mock_network = MagicMock()
        mock_network.host_ip = "172.16.0.1"
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network

        def _copy(source: Path, target: Path) -> None:
            target.write_bytes(source.read_bytes())

        config = sample_config.model_copy(update={"disk_size_mib": 2, "grow_filesystem": True})
        expected_disk = smol_vm.data_dir / "disks" / "vm001.ext4"
        with (
            patch.object(SmolVMManager, "_copy_with_reflink", side_effect=_copy),
            patch.object(
                smol_vm,
                "_grow_raw_ext4_filesystem",
                side_effect=SmolVMError("grow failed"),
            ),
            pytest.raises(SmolVMError, match="grow failed"),
        ):
            smol_vm.create(config)

        assert not expected_disk.exists()
        with pytest.raises(VMNotFoundError):
            smol_vm.get("vm001")

    def test_resize_rejects_shared_disk_mode(
        self,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
    ) -> None:
        """Resize requests must not mutate the caller's base image."""
        config = sample_config.model_copy(update={"disk_mode": "shared", "disk_size_mib": 2})
        with pytest.raises(SmolVMError, match="isolated disk"):
            smol_vm.create(config)

    @patch("smolvm.vm.NetworkManager")
    def test_create_resizes_qemu_overlay_disk(
        self,
        mock_network_class: MagicMock,
        tmp_path: Path,
    ) -> None:
        """QEMU qcow2 overlays are resized with qemu-img."""
        smol_vm = SmolVMManager(
            data_dir=tmp_path / "data-qemu-resize",
            socket_dir=tmp_path / "sockets-qemu-resize",
            backend="qemu",
        )
        mock_network = MagicMock()
        mock_network.generate_mac.return_value = "AA:FC:00:00:00:01"
        mock_network_class.return_value = mock_network
        smol_vm.network = mock_network
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.write_bytes(b"\0" * (1024 * 1024))
        config = VMConfig(
            vm_id="vm-qemu-resize",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
            disk_size_mib=4,
        )
        resize_calls: list[list[str]] = []

        def _fake_qemu_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            del kwargs
            if command[1] == "info":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout='{"virtual-size": 1048576}',
                )
            resize_calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="")

        def _create_overlay(_base: Path, overlay: Path, **_kwargs: object) -> None:
            overlay.touch()

        with (
            patch.object(SmolVMManager, "_create_qemu_overlay_disk", side_effect=_create_overlay),
            patch.object(smol_vm, "_find_qemu_img_binary", return_value=Path("qemu-img")),
            patch("smolvm.vm.subprocess.run", side_effect=_fake_qemu_run),
        ):
            vm_info = smol_vm.create(config)

        expected_disk = smol_vm.data_dir / "disks" / "vm-qemu-resize.qcow2"
        assert vm_info.config.rootfs_path == expected_disk
        assert resize_calls == [["qemu-img", "resize", str(expected_disk), "4M"]]

    def test_qcow2_resize_compares_bytes_not_ceil_mib(
        self,
        smol_vm: SmolVMManager,
        tmp_path: Path,
    ) -> None:
        """A 1 MiB + 1 byte qcow2 should still resize when target is 2 MiB."""
        disk = tmp_path / "disk.qcow2"
        disk.touch()
        calls: list[list[str]] = []

        def _fake_qemu_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            del kwargs
            if command[1] == "info":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout='{"virtual-size": 1048577}',
                )
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="")

        with (
            patch.object(smol_vm, "_find_qemu_img_binary", return_value=Path("qemu-img")),
            patch("smolvm.vm.subprocess.run", side_effect=_fake_qemu_run),
        ):
            smol_vm._resize_qcow2_disk(disk, 2, "vm-qcow2")

        assert calls == [["qemu-img", "resize", str(disk), "2M"]]

    def test_grow_rejects_qcow2_disk(
        self,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
        tmp_path: Path,
    ) -> None:
        """Host-side filesystem growth is raw-ext4 only in this release."""
        qcow2 = tmp_path / "disk.qcow2"
        qcow2.touch()
        config = sample_config.model_copy(update={"rootfs_path": qcow2, "grow_filesystem": True})
        with pytest.raises(SmolVMError, match="qcow2"):
            smol_vm._resize_materialized_rootfs(config)

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


def _info(config: VMConfig, status: VMState, pid: int | None = None) -> VMInfo:
    """Build a VMInfo with a real VMConfig (pydantic strict-validates)."""
    return VMInfo(vm_id=config.vm_id, status=status, config=config, pid=pid)


class TestRefreshStatus:
    """Tests for the cheap per-row liveness check used by ``smolvm list``."""

    def test_running_with_live_pid_unchanged(
        self, smol_vm: SmolVMManager, sample_config: VMConfig
    ) -> None:
        vm_info = _info(sample_config, VMState.RUNNING, pid=12345)
        with patch.object(smol_vm, "_is_process_running", return_value=True):
            result = smol_vm.refresh_status(vm_info)
        assert result is vm_info

    def test_running_with_dead_pid_demoted_to_error(
        self, smol_vm: SmolVMManager, sample_config: VMConfig
    ) -> None:
        vm_info = _info(sample_config, VMState.RUNNING, pid=99999)
        updated = _info(sample_config, VMState.ERROR, pid=None)
        with (
            patch.object(smol_vm, "_is_process_running", return_value=False),
            patch.object(smol_vm.state, "update_vm", return_value=updated) as mock_update,
        ):
            result = smol_vm.refresh_status(vm_info)
        assert result.status == VMState.ERROR
        mock_update.assert_called_once_with(
            sample_config.vm_id, status=VMState.ERROR, clear_pid=True
        )

    def test_paused_with_dead_pid_demoted_to_error(
        self, smol_vm: SmolVMManager, sample_config: VMConfig
    ) -> None:
        vm_info = _info(sample_config, VMState.PAUSED, pid=99999)
        updated = _info(sample_config, VMState.ERROR, pid=None)
        with (
            patch.object(smol_vm, "_is_process_running", return_value=False),
            patch.object(smol_vm.state, "update_vm", return_value=updated),
        ):
            result = smol_vm.refresh_status(vm_info)
        assert result.status == VMState.ERROR

    def test_stopped_not_touched(self, smol_vm: SmolVMManager, sample_config: VMConfig) -> None:
        vm_info = _info(sample_config, VMState.STOPPED, pid=None)
        with (
            patch.object(smol_vm, "_is_process_running") as mock_check,
            patch.object(smol_vm.state, "update_vm") as mock_update,
        ):
            result = smol_vm.refresh_status(vm_info)
        assert result is vm_info
        mock_check.assert_not_called()
        mock_update.assert_not_called()

    def test_running_without_pid_not_touched(
        self, smol_vm: SmolVMManager, sample_config: VMConfig
    ) -> None:
        vm_info = _info(sample_config, VMState.RUNNING, pid=None)
        with (
            patch.object(smol_vm, "_is_process_running") as mock_check,
            patch.object(smol_vm.state, "update_vm") as mock_update,
        ):
            result = smol_vm.refresh_status(vm_info)
        assert result is vm_info
        mock_check.assert_not_called()
        mock_update.assert_not_called()


class TestCrashedVMDetection:
    """Tests that pause/resume surface a useful error when the VM has crashed."""

    def test_resume_reports_crash_when_status_stale_running(
        self, smol_vm: SmolVMManager, sample_config: VMConfig
    ) -> None:
        """DB says RUNNING but PID is dead: resume should raise 'crashed', not 'Cannot resume'."""
        vm_info = _info(sample_config, VMState.RUNNING, pid=99999)
        crashed = _info(sample_config, VMState.ERROR, pid=None)
        with (
            patch.object(smol_vm.state, "get_vm", return_value=vm_info),
            patch.object(smol_vm, "_is_process_running", return_value=False),
            patch.object(smol_vm.state, "update_vm", return_value=crashed),
            pytest.raises(SmolVMError, match="is not running"),
        ):
            smol_vm.resume(sample_config.vm_id)

    def test_resume_original_error_when_status_genuinely_wrong(
        self, smol_vm: SmolVMManager, sample_config: VMConfig
    ) -> None:
        """DB says STOPPED: resume should raise the original 'Cannot resume' error."""
        vm_info = _info(sample_config, VMState.STOPPED, pid=None)
        with (
            patch.object(smol_vm.state, "get_vm", return_value=vm_info),
            pytest.raises(SmolVMError, match="Cannot resume VM in state 'stopped'"),
        ):
            smol_vm.resume(sample_config.vm_id)

    def test_pause_reports_crash_when_runtime_pause_fails_with_dead_pid(
        self, smol_vm: SmolVMManager, sample_config: VMConfig
    ) -> None:
        """pause's QMP call fails: if PID is dead, surface a crash message."""
        vm_info = _info(sample_config, VMState.RUNNING, pid=99999)
        crashed = _info(sample_config, VMState.ERROR, pid=None)
        mock_adapter = MagicMock()
        mock_adapter.pause.side_effect = SmolVMError("Timed out waiting for QMP socket")
        with (
            patch.object(smol_vm.state, "get_vm", return_value=vm_info),
            patch.object(smol_vm, "_runtime_adapter_for_vm", return_value=mock_adapter),
            patch.object(smol_vm, "_is_process_running", return_value=False),
            patch.object(smol_vm.state, "update_vm", return_value=crashed),
            pytest.raises(SmolVMError, match="is not running"),
        ):
            smol_vm.pause(sample_config.vm_id)


class TestResolveBootArgs:
    """Tests for boot-args resolution, including SSH-key cmdline injection.

    Published images don't bake authorized_keys at build time. The launching
    user's pubkey is injected via the kernel cmdline as a base64-encoded
    ``smolvm.authorized_key_b64`` param, which the guest's ``/init`` decodes
    and writes to ``/root/.ssh/authorized_keys`` before sshd starts.
    """

    _ED25519_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBxampleKeyForTestingOnly user@host"

    def _vm_info(
        self,
        smol_vm: SmolVMManager,
        sample_config: VMConfig,
        *,
        ssh_public_key: str | None = None,
        boot_args: str | None = None,
    ) -> VMInfo:
        config_updates: dict[str, object] = {}
        if ssh_public_key is not None:
            config_updates["ssh_public_key"] = ssh_public_key
        if boot_args is not None:
            config_updates["boot_args"] = boot_args
        if config_updates:
            config = sample_config.model_copy(update=config_updates)
        else:
            config = sample_config
        return VMInfo(vm_id=config.vm_id, status=VMState.STOPPED, config=config)

    def test_no_key_means_no_cmdline_injection(
        self, smol_vm: SmolVMManager, sample_config: VMConfig
    ) -> None:
        info = self._vm_info(smol_vm, sample_config)
        assert "smolvm.authorized_key_b64=" not in smol_vm._resolve_boot_args(info)

    def test_key_is_base64_encoded_into_cmdline(
        self, smol_vm: SmolVMManager, sample_config: VMConfig
    ) -> None:
        import base64

        info = self._vm_info(smol_vm, sample_config, ssh_public_key=self._ED25519_KEY)
        args = smol_vm._resolve_boot_args(info)

        token = next(
            (p for p in args.split() if p.startswith("smolvm.authorized_key_b64=")),
            None,
        )
        assert token is not None, args
        encoded = token.split("=", 1)[1]
        # Base64 is space-free — that's the whole point. Round-trip must match.
        assert " " not in encoded
        decoded = base64.b64decode(encoded).decode("utf-8")
        assert decoded == self._ED25519_KEY

    def test_key_in_existing_boot_args_is_not_duplicated(
        self, smol_vm: SmolVMManager, sample_config: VMConfig
    ) -> None:
        info = self._vm_info(
            smol_vm,
            sample_config,
            ssh_public_key=self._ED25519_KEY,
            boot_args="console=ttyS0 smolvm.authorized_key_b64=PRESET",
        )
        args = smol_vm._resolve_boot_args(info)
        tokens = [p for p in args.split() if p.startswith("smolvm.authorized_key_b64=")]
        assert tokens == ["smolvm.authorized_key_b64=PRESET"]

    def test_key_strip_whitespace_before_encoding(
        self, smol_vm: SmolVMManager, sample_config: VMConfig
    ) -> None:
        """Trailing newlines from key files shouldn't end up in the encoded token."""
        import base64

        info = self._vm_info(smol_vm, sample_config, ssh_public_key=f"  {self._ED25519_KEY}\n\n")
        args = smol_vm._resolve_boot_args(info)

        token = next(p for p in args.split() if p.startswith("smolvm.authorized_key_b64="))
        decoded = base64.b64decode(token.split("=", 1)[1]).decode("utf-8")
        assert decoded == self._ED25519_KEY

    def test_init_script_parses_authorized_key_cmdline(self) -> None:
        """The /init script must contain the parser block — keep host + guest in sync."""
        from smolvm.images.builder import ImageBuilder

        script = ImageBuilder()._default_init_script()
        assert "smolvm.authorized_key_b64=" in script
        assert "base64 -d" in script
        assert "/root/.ssh/authorized_keys" in script
        # Parser must run BEFORE sshd starts, otherwise the new key isn't picked up.
        authkey_at = script.find("ssh-authkey-inject-start")
        sshd_at = script.find("sshd-start")
        assert 0 <= authkey_at < sshd_at, "key install must precede sshd start"
