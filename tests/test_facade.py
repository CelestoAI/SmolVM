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

"""Tests for SmolVM VM facade module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.cloud_init import seed_cache_key
from smolvm.exceptions import (
    CommandExecutionUnavailableError,
    OperationTimeoutError,
    SmolVMError,
)
from smolvm.facade import SmolVM, _build_auto_config
from smolvm.types import GuestOS, VMConfig, VMState


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
        mem_size_mib=512,
        kernel_path=kernel,
        rootfs_path=rootfs,
    )


class TestVMInit:
    """Tests for VM initialization."""

    @patch("smolvm.facade.SmolVMManager")
    def test_create_with_config(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test creating a VM with a config."""
        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)

        assert vm.vm_id == "vm001"
        mock_sdk.create.assert_called_once_with(sample_config)

    @patch("smolvm.facade.SmolVMManager")
    def test_create_with_config_without_vm_id(
        self,
        mock_sdk_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test creating a VM when VMConfig omits vm_id."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config = VMConfig(
            kernel_path=kernel,
            rootfs_path=rootfs,
        )

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id=config.vm_id, status=VMState.CREATED)
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(config)

        assert vm.vm_id == config.vm_id
        assert vm.vm_id.startswith("vm-")
        mock_sdk.create.assert_called_once_with(config)

    def test_both_config_and_id_raises(self, sample_config: VMConfig) -> None:
        """Test that passing both config and vm_id raises ValueError."""
        with pytest.raises(ValueError, match="not both"):
            SmolVM(sample_config, vm_id="vm001")

    @patch("smolvm.facade.SmolVMManager")
    @patch("smolvm.build.ImageBuilder")
    @patch("smolvm.utils.ensure_ssh_key")
    @patch("smolvm.backends.platform.system", return_value="Linux")
    def test_neither_config_nor_id_autoconfigures(
        self,
        _: MagicMock,
        mock_ensure_ssh_key: MagicMock,
        mock_builder_cls: MagicMock,
        mock_sdk_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test auto-configuration mode when neither config nor vm_id is provided."""
        kernel = tmp_path / "auto-kernel"
        rootfs = tmp_path / "auto-rootfs.ext4"
        private_key = tmp_path / "id_ed25519"
        public_key = tmp_path / "id_ed25519.pub"
        kernel.touch()
        rootfs.touch()
        private_key.touch()
        public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey user@test\n")

        mock_ensure_ssh_key.return_value = (private_key, public_key)
        mock_builder = MagicMock()
        mock_builder.build_alpine_ssh_key.return_value = (kernel, rootfs)
        mock_builder_cls.return_value = mock_builder

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM()

        assert vm.vm_id.startswith("vm-")
        mock_builder.build_alpine_ssh_key.assert_called_once()
        assert mock_builder.build_alpine_ssh_key.call_args.args[0] == public_key
        assert mock_builder.build_alpine_ssh_key.call_args.kwargs["rootfs_size_mb"] == 512
        mock_sdk.create.assert_called_once()
        created_config = mock_sdk.create.call_args[0][0]
        assert "init=/init" in created_config.boot_args
        assert created_config.mem_size_mib == 512

    @patch("smolvm.facade.SmolVMManager")
    @patch("smolvm.build.ImageBuilder")
    @patch("smolvm.utils.ensure_ssh_key")
    @patch("smolvm.backends.platform.system", return_value="Linux")
    def test_autoconfigure_with_custom_mem_and_disk(
        self,
        _: MagicMock,
        mock_ensure_ssh_key: MagicMock,
        mock_builder_cls: MagicMock,
        mock_sdk_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test auto-configuration accepts custom memory and disk size."""
        kernel = tmp_path / "auto-kernel"
        rootfs = tmp_path / "auto-rootfs.ext4"
        private_key = tmp_path / "id_ed25519"
        public_key = tmp_path / "id_ed25519.pub"
        kernel.touch()
        rootfs.touch()
        private_key.touch()
        public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey user@test\n")

        mock_ensure_ssh_key.return_value = (private_key, public_key)
        mock_builder = MagicMock()
        mock_builder.build_alpine_ssh_key.return_value = (kernel, rootfs)
        mock_builder_cls.return_value = mock_builder

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(mem_size_mib=2048, disk_size_mib=4096)

        assert vm.vm_id.startswith("vm-")
        mock_builder.build_alpine_ssh_key.assert_called_once()
        assert mock_builder.build_alpine_ssh_key.call_args.kwargs["rootfs_size_mb"] == 4096
        mock_sdk.create.assert_called_once()
        created_config = mock_sdk.create.call_args[0][0]
        assert created_config.mem_size_mib == 2048

    @patch("smolvm.facade.SmolVMManager")
    @patch("smolvm.build.ImageBuilder")
    @patch("smolvm.utils.ensure_ssh_key")
    def test_autoconfigure_with_debian_uses_debian_builder(
        self,
        mock_ensure_ssh_key: MagicMock,
        mock_builder_cls: MagicMock,
        mock_sdk_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Debian auto-config should use the Debian builder and defaults."""
        kernel = tmp_path / "auto-kernel"
        rootfs = tmp_path / "auto-rootfs.ext4"
        private_key = tmp_path / "id_ed25519"
        public_key = tmp_path / "id_ed25519.pub"
        kernel.touch()
        rootfs.touch()
        private_key.touch()
        public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey user@test\n")

        mock_ensure_ssh_key.return_value = (private_key, public_key)
        mock_builder = MagicMock()
        mock_builder.build_debian_ssh_key.return_value = (kernel, rootfs)
        mock_builder_cls.return_value = mock_builder

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(os="debian")

        assert vm.vm_id.startswith("vm-")
        mock_builder.build_debian_ssh_key.assert_called_once()
        assert mock_builder.build_debian_ssh_key.call_args.args[0] == public_key
        assert mock_builder.build_debian_ssh_key.call_args.kwargs["rootfs_size_mb"] == 2048
        mock_builder.build_alpine_ssh_key.assert_not_called()
        created_config = mock_sdk.create.call_args[0][0]
        assert created_config.mem_size_mib == 512

    @patch("smolvm.facade.SmolVMManager")
    @patch("smolvm.build.ImageBuilder")
    @patch("smolvm.utils.ensure_ssh_key")
    def test_autoconfigure_with_debian_enum_works(
        self,
        mock_ensure_ssh_key: MagicMock,
        mock_builder_cls: MagicMock,
        mock_sdk_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """GuestOS enum values should work for auto-config."""
        kernel = tmp_path / "auto-kernel"
        rootfs = tmp_path / "auto-rootfs.ext4"
        private_key = tmp_path / "id_ed25519"
        public_key = tmp_path / "id_ed25519.pub"
        kernel.touch()
        rootfs.touch()
        private_key.touch()
        public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey user@test\n")

        mock_ensure_ssh_key.return_value = (private_key, public_key)
        mock_builder = MagicMock()
        mock_builder.build_debian_ssh_key.return_value = (kernel, rootfs)
        mock_builder_cls.return_value = mock_builder

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk_cls.return_value = mock_sdk

        SmolVM(os=GuestOS.DEBIAN)

        mock_builder.build_debian_ssh_key.assert_called_once()

    @patch("smolvm.build.ImageBuilder")
    @patch("smolvm.utils.ensure_ssh_key")
    def test_build_auto_config_debian_disk_override(
        self,
        mock_ensure_ssh_key: MagicMock,
        mock_builder_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Explicit disk sizes should override Debian defaults."""
        kernel = tmp_path / "auto-kernel"
        rootfs = tmp_path / "auto-rootfs.ext4"
        private_key = tmp_path / "id_ed25519"
        public_key = tmp_path / "id_ed25519.pub"
        kernel.touch()
        rootfs.touch()
        private_key.touch()
        public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey user@test\n")

        mock_ensure_ssh_key.return_value = (private_key, public_key)
        mock_builder = MagicMock()
        mock_builder.build_debian_ssh_key.return_value = (kernel, rootfs)
        mock_builder_cls.return_value = mock_builder

        _build_auto_config(os="debian", disk_size_mib=4096)

        assert mock_builder.build_debian_ssh_key.call_args.kwargs["rootfs_size_mb"] == 4096

    def test_custom_auto_sizing_with_config_raises(self, sample_config: VMConfig) -> None:
        """Custom auto sizing options are only valid in zero-config mode."""
        with pytest.raises(ValueError, match="auto-config mode"):
            SmolVM(sample_config, mem_size_mib=1024)

    def test_os_with_config_raises(self, sample_config: VMConfig) -> None:
        """Guest OS selection is only valid in zero-config mode."""
        with pytest.raises(ValueError, match="auto-config mode"):
            SmolVM(sample_config, os="debian")

    def test_os_with_vm_id_raises(self) -> None:
        """Guest OS selection should be rejected when reconnecting to a VM."""
        with pytest.raises(ValueError, match="auto-config mode"):
            SmolVM(vm_id="vm001", os="debian")

    def test_invalid_os_raises(self) -> None:
        """Unsupported guest OS names should raise a helpful error."""
        with pytest.raises(ValueError, match="Valid values: alpine, debian, ubuntu"):
            _build_auto_config(os="fedora")

    @patch("smolvm.build.ImageBuilder")
    @patch("smolvm.utils.ensure_ssh_key")
    @patch("smolvm.backends.platform.system", return_value="Linux")
    def test_named_auto_config_preserves_vm_name(
        self,
        _: MagicMock,
        mock_ensure_ssh_key: MagicMock,
        mock_builder_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Named auto-config should keep the caller-supplied VM name."""
        kernel = tmp_path / "auto-kernel"
        rootfs = tmp_path / "auto-rootfs.ext4"
        private_key = tmp_path / "id_ed25519"
        public_key = tmp_path / "id_ed25519.pub"
        kernel.touch()
        rootfs.touch()
        private_key.touch()
        public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey user@test\n")

        mock_ensure_ssh_key.return_value = (private_key, public_key)
        mock_builder = MagicMock()
        mock_builder.build_alpine_ssh_key.return_value = (kernel, rootfs)
        mock_builder_cls.return_value = mock_builder

        config, ssh_key_path = _build_auto_config(vm_name="project-spacex")

        assert config.vm_id == "project-spacex"
        assert ssh_key_path == str(private_key)
        assert "init=/init" in config.boot_args
        mock_builder.build_alpine_ssh_key.assert_called_once()

    @patch("smolvm.facade.platform.machine", return_value="arm64")
    @patch("smolvm.facade.build_seed_iso")
    @patch("smolvm.facade.ImageManager")
    @patch("smolvm.utils.ensure_ssh_key")
    def test_named_auto_config_qemu_keeps_backend_specific_settings(
        self,
        mock_ensure_ssh_key: MagicMock,
        mock_image_manager_cls: MagicMock,
        mock_build_seed_iso: MagicMock,
        _: MagicMock,
        tmp_path: Path,
    ) -> None:
        """QEMU auto-config should use the prebuilt image path."""
        kernel = tmp_path / "vmlinuz"
        initrd = tmp_path / "initrd"
        rootfs = tmp_path / "rootfs.qcow2"
        private_key = tmp_path / "id_ed25519"
        public_key = tmp_path / "id_ed25519.pub"
        kernel.touch()
        initrd.touch()
        rootfs.touch()
        private_key.touch()
        public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey user@test\n")

        mock_ensure_ssh_key.return_value = (private_key, public_key)
        mock_build_seed_iso.side_effect = lambda path, **kwargs: (
            path.parent.mkdir(parents=True, exist_ok=True),
            path.touch(),
        )[-1]
        mock_image_manager = MagicMock()
        mock_image_manager.cache_dir = tmp_path
        mock_image_manager.ensure_image.return_value = MagicMock(
            kernel_path=kernel,
            initrd_path=initrd,
            rootfs_path=rootfs,
        )
        mock_image_manager_cls.return_value = mock_image_manager

        config, _ = _build_auto_config(vm_name="project-spacex", backend="qemu")

        assert config.backend == "qemu"
        assert config.initrd_path == initrd
        assert config.ssh_capable is True
        assert "root=LABEL=cloudimg-rootfs" in config.boot_args
        assert config.extra_drives[0].suffix == ".iso"
        mock_image_manager.ensure_image.assert_called_once_with("ubuntu-jammy-qemu-aarch64")

    @patch("smolvm.facade.platform.machine", return_value="arm64")
    @patch("smolvm.facade.build_seed_iso")
    @patch("smolvm.facade.ImageManager")
    @patch("smolvm.utils.ensure_ssh_key")
    def test_named_auto_config_qemu_uses_explicit_ssh_key_for_seed(
        self,
        mock_ensure_ssh_key: MagicMock,
        mock_image_manager_cls: MagicMock,
        mock_build_seed_iso: MagicMock,
        _: MagicMock,
        tmp_path: Path,
    ) -> None:
        """QEMU auto-config should honor an explicit SSH key when building the seed ISO."""
        kernel = tmp_path / "vmlinuz"
        initrd = tmp_path / "initrd"
        rootfs = tmp_path / "rootfs.qcow2"
        default_private = tmp_path / "id_ed25519"
        default_public = tmp_path / "id_ed25519.pub"
        custom_private = tmp_path / "custom_id_ed25519"
        custom_public = tmp_path / "custom_id_ed25519.pub"
        kernel.touch()
        initrd.touch()
        rootfs.touch()
        default_private.touch()
        default_public.write_text("ssh-ed25519 AAAAC3NzaDefault user@test\n")
        custom_private.touch()
        custom_public.write_text("ssh-ed25519 AAAAC3NzaCustom user@test\n")

        mock_ensure_ssh_key.return_value = (default_private, default_public)
        mock_build_seed_iso.side_effect = lambda path, **kwargs: (
            path.parent.mkdir(parents=True, exist_ok=True),
            path.touch(),
        )[-1]
        mock_image_manager = MagicMock()
        mock_image_manager.cache_dir = tmp_path
        mock_image_manager.ensure_image.return_value = MagicMock(
            kernel_path=kernel,
            initrd_path=initrd,
            rootfs_path=rootfs,
        )
        mock_image_manager_cls.return_value = mock_image_manager

        config, ssh_key_path = _build_auto_config(
            vm_name="project-spacex",
            backend="qemu",
            ssh_key_path=str(custom_private),
        )

        expected_seed_name = (
            seed_cache_key(
                ssh_public_key=custom_public.read_text().strip(),
                instance_id="smolvm-20260320",
                hostname="smolvm",
            )
            + ".iso"
        )
        assert ssh_key_path == str(custom_private)
        assert config.extra_drives[0].name == expected_seed_name
        assert config.ssh_capable is True
        assert "AAAAC3NzaCustom" in mock_build_seed_iso.call_args.kwargs["user_data"]
        mock_ensure_ssh_key.assert_not_called()

    @patch("smolvm.facade.platform.machine", return_value="arm64")
    @patch("smolvm.build.ImageBuilder")
    @patch("smolvm.utils.ensure_ssh_key")
    def test_named_debian_auto_config_qemu_keeps_backend_specific_settings(
        self,
        mock_ensure_ssh_key: MagicMock,
        mock_builder_cls: MagicMock,
        _: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Debian auto-config should use the same backend-specific cache naming."""
        kernel = tmp_path / "auto-kernel"
        rootfs = tmp_path / "auto-rootfs.ext4"
        private_key = tmp_path / "id_ed25519"
        public_key = tmp_path / "id_ed25519.pub"
        kernel.touch()
        rootfs.touch()
        private_key.touch()
        public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey user@test\n")

        mock_ensure_ssh_key.return_value = (private_key, public_key)
        mock_builder = MagicMock()
        mock_builder.build_debian_ssh_key.return_value = (kernel, rootfs)
        mock_builder_cls.return_value = mock_builder

        config, _ = _build_auto_config(vm_name="project-spacex", os="debian", backend="qemu")

        assert config.backend == "qemu"
        assert config.boot_args == "console=ttyAMA0 reboot=k panic=1 init=/init"
        assert (
            mock_builder.build_debian_ssh_key.call_args.kwargs["name"]
            == "debian-ssh-key-aarch64"
        )

    @patch("smolvm.facade.SmolVMManager")
    def test_from_id(self, mock_sdk_cls: MagicMock) -> None:
        """Test reconnecting to an existing VM by ID."""
        mock_sdk = MagicMock()
        mock_sdk.get.return_value = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        mock_sdk_cls.from_id.return_value = mock_sdk

        vm = SmolVM.from_id("vm001")

        assert vm.vm_id == "vm001"
        mock_sdk_cls.from_id.assert_called_once()

    @patch("smolvm.facade.SmolVMManager")
    def test_from_snapshot(self, mock_sdk_cls: MagicMock) -> None:
        """from_snapshot() should restore the snapshot before attaching to the VM."""
        restore_manager = MagicMock()
        restore_manager.__enter__.return_value = restore_manager
        restore_manager.get_snapshot.return_value = MagicMock(backend="firecracker")
        restore_manager.restore_snapshot.return_value = MagicMock(
            vm_id="vm001",
            status=VMState.PAUSED,
            config=MagicMock(backend="firecracker"),
        )
        attach_manager = MagicMock()
        attach_manager.get.return_value = MagicMock(vm_id="vm001", status=VMState.PAUSED)

        mock_sdk_cls.return_value = restore_manager
        mock_sdk_cls.from_id.return_value = attach_manager

        vm = SmolVM.from_snapshot("snap-001")

        assert vm.vm_id == "vm001"
        restore_manager.restore_snapshot.assert_called_once_with(
            "snap-001",
            resume_vm=False,
            force=False,
        )
        mock_sdk_cls.from_id.assert_called_once()

    @patch("smolvm.facade.SmolVMManager")
    def test_from_snapshot_rejects_mismatched_backend(self, mock_sdk_cls: MagicMock) -> None:
        """from_snapshot() should reject an explicit backend that disagrees with the snapshot."""
        restore_manager = MagicMock()
        restore_manager.__enter__.return_value = restore_manager
        restore_manager.get_snapshot.return_value = MagicMock(backend="firecracker")
        mock_sdk_cls.return_value = restore_manager

        with pytest.raises(SmolVMError, match="does not match the snapshot backend"):
            SmolVM.from_snapshot("snap-001", backend="qemu")


class TestVMLifecycle:
    """Tests for VM lifecycle operations."""

    @patch("smolvm.facade.SmolVMManager")
    def test_start_returns_self(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test that start() returns self for chaining."""
        mock_sdk = MagicMock()
        mock_info = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_info.config.env_vars = {}

        running_info = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        running_info.config.env_vars = {}

        mock_sdk.create.return_value = mock_info
        mock_sdk.start.return_value = running_info
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        result = vm.start()

        assert result is vm
        mock_sdk.start.assert_called_once()

    @patch("smolvm.facade.SmolVMManager")
    def test_start_noop_if_already_running(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test start() is a no-op when VM is already running."""
        mock_sdk = MagicMock()
        running_info = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        running_info.config.env_vars = {}
        mock_sdk.create.return_value = running_info
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        result = vm.start()

        assert result is vm
        mock_sdk.start.assert_not_called()

    @patch("smolvm.facade.SmolVMManager")
    def test_start_resumes_paused_vm(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """start() should resume paused VMs."""
        mock_sdk = MagicMock()
        paused_info = MagicMock(vm_id="vm001", status=VMState.PAUSED)
        paused_info.config.env_vars = {}
        running_info = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        running_info.config.env_vars = {}
        mock_sdk.create.return_value = paused_info
        mock_sdk.resume.return_value = running_info
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        result = vm.start()

        assert result is vm
        mock_sdk.resume.assert_called_once_with("vm001")
        mock_sdk.start.assert_not_called()

    @patch("smolvm.facade.SmolVMManager")
    def test_stop_returns_self(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test that stop() returns self for chaining."""
        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.stop.return_value = MagicMock(vm_id="vm001", status=VMState.STOPPED)
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        result = vm.stop()

        assert result is vm
        mock_sdk.stop.assert_called_once()

    @patch("smolvm.facade.SmolVMManager")
    def test_delete(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test deleting a VM."""
        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        vm.delete()

        mock_sdk.delete.assert_called_once_with("vm001")


class TestVMRun:
    """Tests for command execution on the VM."""

    @patch("smolvm.facade.SmolVMManager")
    def test_can_run_commands_requires_explicit_ssh_capability_for_initrd(
        self,
        mock_sdk_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """An initrd alone should not imply SSH command execution support."""
        kernel = tmp_path / "vmlinuz"
        initrd = tmp_path / "initrd"
        rootfs = tmp_path / "rootfs.qcow2"
        kernel.touch()
        initrd.touch()
        rootfs.touch()
        config = VMConfig(
            vm_id="vm001",
            kernel_path=kernel,
            initrd_path=initrd,
            rootfs_path=rootfs,
        )

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.CREATED
        mock_info.config = config
        mock_sdk = MagicMock()
        mock_sdk.create.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(config)

        assert vm.can_run_commands() is False

    @patch("smolvm.facade.SmolVMManager")
    def test_can_run_commands_allows_initrd_only_when_explicitly_ssh_capable(
        self,
        mock_sdk_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Initrd-backed configs must opt into SSH capability explicitly."""
        kernel = tmp_path / "vmlinuz"
        initrd = tmp_path / "initrd"
        rootfs = tmp_path / "rootfs.qcow2"
        kernel.touch()
        initrd.touch()
        rootfs.touch()
        config = VMConfig(
            vm_id="vm001",
            kernel_path=kernel,
            initrd_path=initrd,
            rootfs_path=rootfs,
            ssh_capable=True,
        )

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.CREATED
        mock_info.config = config
        mock_sdk = MagicMock()
        mock_sdk.create.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(config)

        assert vm.can_run_commands() is True

    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVMManager")
    def test_run_on_running_vm(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test run() works on a running VM."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network
        mock_info.config.boot_args = "console=ttyS0 reboot=k panic=1 pci=off init=/init"

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        mock_ssh = MagicMock()
        mock_ssh.run.return_value = MagicMock(exit_code=0, stdout="ok\n", stderr="")
        mock_ssh_cls.return_value = mock_ssh

        vm = SmolVM(sample_config)
        result = vm.run("echo ok")

        assert result.exit_code == 0
        mock_ssh.wait_for_ssh.assert_called_once()
        wait_timeout = mock_ssh.wait_for_ssh.call_args.kwargs["timeout"]
        assert 0.5 <= wait_timeout <= 30.0
        mock_ssh.run.assert_called_once_with("echo ok", timeout=30, shell="login")

    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVMManager")
    def test_run_raw_shell_mode(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test run() can bypass login-shell wrapping via raw mode."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network
        mock_info.config.boot_args = "console=ttyS0 reboot=k panic=1 pci=off init=/init"

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        mock_ssh = MagicMock()
        mock_ssh.run.return_value = MagicMock(exit_code=0, stdout="ok\n", stderr="")
        mock_ssh_cls.return_value = mock_ssh

        vm = SmolVM(sample_config)
        vm.run("echo ok", shell="raw")

        mock_ssh.wait_for_ssh.assert_called_once()
        wait_timeout = mock_ssh.wait_for_ssh.call_args.kwargs["timeout"]
        assert 0.5 <= wait_timeout <= 30.0
        mock_ssh.run.assert_called_once_with("echo ok", timeout=30, shell="raw")

    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVMManager")
    def test_run_waits_for_ssh_once(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test run() performs one-time SSH readiness wait."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network
        mock_info.config.boot_args = "console=ttyS0 reboot=k panic=1 pci=off init=/init"

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        mock_ssh = MagicMock()
        mock_ssh.run.return_value = MagicMock(exit_code=0, stdout="ok\n", stderr="")
        mock_ssh_cls.return_value = mock_ssh

        vm = SmolVM(sample_config)
        vm.run("echo one")
        vm.run("echo two")

        mock_ssh.wait_for_ssh.assert_called_once()
        wait_timeout = mock_ssh.wait_for_ssh.call_args.kwargs["timeout"]
        assert 0.5 <= wait_timeout <= 30.0
        assert mock_ssh.run.call_count == 2


    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVMManager")
    def test_run_falls_back_to_guest_ip_when_localhost_unreachable(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """run() should fallback to guest IP when localhost forwarding is down."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"
        mock_network.ssh_host_port = 2200

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network
        mock_info.config.boot_args = "console=ttyS0 reboot=k panic=1 pci=off init=/init"

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        localhost_client = MagicMock()
        localhost_client.wait_for_ssh.side_effect = OperationTimeoutError("wait_for_ssh", 15.0)
        guest_client = MagicMock()
        guest_client.run.return_value = MagicMock(exit_code=0, stdout="ok\n", stderr="")
        mock_ssh_cls.side_effect = [localhost_client, guest_client]

        vm = SmolVM(sample_config)
        result = vm.run("echo ok")

        assert result.exit_code == 0
        assert mock_ssh_cls.call_count == 2
        assert mock_ssh_cls.call_args_list[0].kwargs["host"] == "127.0.0.1"
        assert mock_ssh_cls.call_args_list[0].kwargs["port"] == 2200
        assert mock_ssh_cls.call_args_list[1].kwargs["host"] == "172.16.0.2"
        assert mock_ssh_cls.call_args_list[1].kwargs["port"] == 22
        guest_client.wait_for_ssh.assert_called_once()
        guest_client.run.assert_called_once_with("echo ok", timeout=30, shell="login")

    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVMManager")
    def test_wait_for_ssh_falls_back_to_guest_ip_when_localhost_unreachable(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """wait_for_ssh() should fallback from localhost to guest IP."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"
        mock_network.ssh_host_port = 2200

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network
        mock_info.config.boot_args = "console=ttyS0 reboot=k panic=1 pci=off init=/init"

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        localhost_client = MagicMock()
        localhost_client.wait_for_ssh.side_effect = OperationTimeoutError("wait_for_ssh", 10.0)
        guest_client = MagicMock()
        mock_ssh_cls.side_effect = [localhost_client, guest_client]

        vm = SmolVM(sample_config)
        vm.wait_for_ssh(timeout=20.0)

        assert mock_ssh_cls.call_count == 2
        localhost_client.wait_for_ssh.assert_called_once()
        guest_client.wait_for_ssh.assert_called_once()
        assert vm._ssh is guest_client
        assert vm._ssh_ready is True

    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVMManager")
    def test_wait_for_ssh_falls_back_to_default_smolvm_key_when_no_key_configured(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        sample_config: VMConfig,
        tmp_path: Path,
    ) -> None:
        """wait_for_ssh() without an explicit key should retry with ~/.smolvm/keys/id_ed25519.

        Regression test for: smolvm ssh <name> failing with 'Authentication failed'
        after smolvm create, because from_id() sets ssh_key_path=None but the VM
        was provisioned with the default SmolVM key.
        """
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"
        mock_network.ssh_host_port = 2201

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network
        mock_info.config.boot_args = "console=ttyS0 reboot=k panic=1 pci=off init=/init"

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        default_key_path = tmp_path / "keys" / "id_ed25519"
        default_key_path.parent.mkdir(parents=True)
        default_key_path.touch()
        (default_key_path.parent / "id_ed25519.pub").touch()

        # Attempt order: (127.0.0.1:2201, None) → (172.16.0.2:22, None) → (127.0.0.1:2201, key)
        # First two attempts (no key / agent auth) fail; third (default smolvm key) succeeds.
        no_key_client_1 = MagicMock()
        no_key_client_1.host = "127.0.0.1"
        no_key_client_1.port = 2201
        no_key_client_1.key_path = None
        no_key_client_1.wait_for_ssh.side_effect = OperationTimeoutError("wait_for_ssh", 10.0)

        no_key_client_2 = MagicMock()
        no_key_client_2.host = "172.16.0.2"
        no_key_client_2.port = 22
        no_key_client_2.key_path = None
        no_key_client_2.wait_for_ssh.side_effect = OperationTimeoutError("wait_for_ssh", 10.0)

        key_client = MagicMock()
        key_client.host = "127.0.0.1"
        key_client.port = 2201
        key_client.key_path = str(default_key_path)

        mock_ssh_cls.side_effect = [no_key_client_1, no_key_client_2, key_client]

        vm = SmolVM(sample_config)

        with patch("smolvm.utils.ensure_ssh_key", return_value=(default_key_path, default_key_path.parent / "id_ed25519.pub")):
            vm.wait_for_ssh(timeout=30.0)

        # Should have tried the default smolvm key after both no-key attempts failed
        assert mock_ssh_cls.call_count == 3
        third_call_kwargs = mock_ssh_cls.call_args_list[2].kwargs
        assert third_call_kwargs.get("key_path") == str(default_key_path)
        assert vm._ssh is key_client
        assert vm._ssh_ready is True

    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVMManager")
    def test_run_on_non_ssh_boot_profile_raises_clear_error(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test run() fails fast when boot profile is not SSH-capable."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network
        mock_info.config.boot_args = "console=ttyS0 reboot=k panic=1 pci=off"

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        with pytest.raises(CommandExecutionUnavailableError, match="SSH-capable boot path"):
            vm.run("echo test")

        mock_ssh_cls.assert_not_called()

    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVMManager")
    def test_run_maps_ssh_readiness_timeout_to_clear_error(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test run() surfaces readiness timeout as command-unavailable error."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network
        mock_info.config.boot_args = "console=ttyS0 reboot=k panic=1 pci=off init=/init"

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        mock_ssh = MagicMock()
        mock_ssh.wait_for_ssh.side_effect = OperationTimeoutError("wait_for_ssh", 30.0)
        mock_ssh_cls.return_value = mock_ssh

        vm = SmolVM(sample_config)
        with pytest.raises(CommandExecutionUnavailableError, match="SSH did not become ready"):
            vm.run("echo test")

        mock_ssh.run.assert_not_called()

    @patch("smolvm.facade.SmolVMManager")
    def test_run_on_stopped_vm_raises(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test run() raises when VM is not running."""
        mock_info = MagicMock()
        mock_info.status = VMState.STOPPED

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        with pytest.raises(SmolVMError, match="VM is stopped"):
            vm.run("echo test")


@pytest.mark.skip(reason="Fails in macOS secure sandboxes due to bind restrictions")
class TestVMLocalExpose:
    """Tests for localhost-only port exposure."""

    @patch("smolvm.facade.SmolVMManager")
    @patch("smolvm.facade.SmolVM._find_available_local_port", return_value=18081)
    @patch("smolvm.facade.SmolVM._probe_local_forward", return_value=True)
    def test_expose_local_with_explicit_host_port(
        self,
        _mock_probe: MagicMock,
        _mock_find_port: MagicMock,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test exposing a guest port on localhost with explicit host port."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk.network = MagicMock()
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        host_port = vm.expose_local(guest_port=8080, host_port=18080)

        assert host_port == 18080
        mock_sdk.network.setup_local_port_forward.assert_called_once_with(
            vm_id="vm001",
            guest_ip="172.16.0.2",
            host_port=18080,
            guest_port=8080,
        )

    @patch("smolvm.facade.SmolVMManager")
    @patch("smolvm.facade.SmolVM._find_available_local_port", return_value=18081)
    @patch("smolvm.facade.SmolVM._probe_local_forward", return_value=True)
    @patch("smolvm.facade.SmolVM._find_available_local_port", side_effect=[18081, 18082])
    def test_expose_local_auto_host_port(
        self,
        mock_find_port: MagicMock,
        _mock_probe: MagicMock,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test host port auto-selection for localhost forwarding."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk.network = MagicMock()
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        host_port = vm.expose_local(guest_port=8080)

        assert host_port == 18081
        assert mock_find_port.call_count == 2
        mock_sdk.network.setup_local_port_forward.assert_called_once()

    @patch("smolvm.facade.SmolVMManager")
    def test_expose_local_requires_running_vm(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test expose_local() fails when VM is not running."""
        mock_info = MagicMock()
        mock_info.status = VMState.STOPPED
        mock_info.network = None

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk.network = MagicMock()
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        with pytest.raises(SmolVMError, match="VM is stopped"):
            vm.expose_local(guest_port=8080, host_port=18080)

    @patch("smolvm.facade.SmolVMManager")
    @patch("smolvm.facade.SmolVM._find_available_local_port", return_value=18081)
    @patch("smolvm.facade.SmolVM._probe_local_forward", return_value=True)
    def test_stop_cleans_local_forwards(
        self,
        _mock_probe: MagicMock,
        _mock_find_port: MagicMock,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """stop() should remove local forwards configured via expose_local()."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"

        running_info = MagicMock()
        running_info.vm_id = "vm001"
        running_info.status = VMState.RUNNING
        running_info.network = mock_network

        stopped_info = MagicMock()
        stopped_info.vm_id = "vm001"
        stopped_info.status = VMState.STOPPED
        stopped_info.network = mock_network

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = running_info
        mock_sdk.stop.return_value = stopped_info
        mock_sdk.network = MagicMock()
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        vm.expose_local(guest_port=8080, host_port=18080)
        vm.stop()

        mock_sdk.network.cleanup_local_port_forward.assert_called_once_with(
            vm_id="vm001",
            guest_ip="172.16.0.2",
            host_port=18080,
            guest_port=8080,
        )

    @patch("smolvm.facade.SmolVMManager")
    @patch("smolvm.facade.SmolVM._start_local_tunnel")
    @patch("smolvm.facade.SmolVM._probe_local_forward", return_value=False)
    def test_expose_local_falls_back_to_ssh_tunnel(
        self,
        _mock_probe: MagicMock,
        mock_start_tunnel: MagicMock,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Falls back to SSH tunnel when nftables local path is unreachable."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk.network = MagicMock()
        mock_sdk_cls.return_value = mock_sdk

        tunnel_proc = MagicMock()
        mock_start_tunnel.return_value = tunnel_proc

        vm = SmolVM(sample_config)
        host_port = vm.expose_local(guest_port=8080, host_port=18080)

        assert host_port == 18080
        mock_start_tunnel.assert_called_once_with(host_port=18080, guest_port=8080)
        mock_sdk.network.setup_local_port_forward.assert_called_once_with(
            vm_id="vm001",
            guest_ip="172.16.0.2",
            host_port=18080,
            guest_port=8080,
        )
        mock_sdk.network.cleanup_local_port_forward.assert_called_once_with(
            vm_id="vm001",
            guest_ip="172.16.0.2",
            host_port=18080,
            guest_port=8080,
        )

    @patch("smolvm.facade.SmolVMManager")
    @patch("smolvm.facade.SmolVM._start_local_tunnel")
    def test_expose_local_skips_nftables_for_qemu_backend(
        self,
        mock_start_tunnel: MagicMock,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """QEMU localhost exposure should go straight to SSH tunneling."""
        mock_network = MagicMock()
        mock_network.guest_ip = "10.0.2.15"

        mock_config = MagicMock()
        mock_config.backend = "qemu"

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network
        mock_info.config = mock_config

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk.network = MagicMock()
        mock_sdk_cls.return_value = mock_sdk

        tunnel_proc = MagicMock()
        mock_start_tunnel.return_value = tunnel_proc

        vm = SmolVM(sample_config)
        host_port = vm.expose_local(guest_port=8080, host_port=18080)

        assert host_port == 18080
        mock_start_tunnel.assert_called_once_with(host_port=18080, guest_port=8080)
        mock_sdk.network.setup_local_port_forward.assert_not_called()
        mock_sdk.network.cleanup_local_port_forward.assert_not_called()

    @patch("smolvm.facade.SmolVMManager")
    @patch("smolvm.facade.SmolVM._allocate_local_port", return_value=18081)
    @patch("smolvm.facade.SmolVM._start_local_tunnel")
    @patch("smolvm.facade.SmolVM._probe_local_forward", return_value=False)
    def test_expose_local_retries_with_fallback_port(
        self,
        _mock_probe: MagicMock,
        mock_start_tunnel: MagicMock,
        _mock_allocate: MagicMock,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """If the requested host port fails, expose_local retries once with fallback."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk.network = MagicMock()
        mock_sdk_cls.return_value = mock_sdk

        tunnel_proc = MagicMock()
        mock_start_tunnel.side_effect = [SmolVMError("first failed"), tunnel_proc]

        vm = SmolVM(sample_config)
        host_port = vm.expose_local(guest_port=8080, host_port=18080)

        assert host_port == 18081
        assert mock_start_tunnel.call_count == 2
        first_call = mock_start_tunnel.call_args_list[0]
        second_call = mock_start_tunnel.call_args_list[1]
        assert first_call.kwargs == {"host_port": 18080, "guest_port": 8080}
        assert second_call.kwargs == {"host_port": 18081, "guest_port": 8080}

    @patch("smolvm.facade.SmolVMManager")
    @patch("smolvm.facade.SmolVM._stop_local_tunnel")
    @patch("smolvm.facade.SmolVM._start_local_tunnel")
    @patch("smolvm.facade.SmolVM._probe_local_forward", return_value=False)
    def test_unexpose_local_cleans_ssh_tunnel_transport(
        self,
        _mock_probe: MagicMock,
        mock_start_tunnel: MagicMock,
        mock_stop_tunnel: MagicMock,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """unexpose_local() stops tracked SSH tunnel forwards."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk.network = MagicMock()
        mock_sdk_cls.return_value = mock_sdk

        tunnel_proc = MagicMock()
        mock_start_tunnel.return_value = tunnel_proc

        vm = SmolVM(sample_config)
        vm.expose_local(guest_port=8080, host_port=18080)
        vm.unexpose_local(host_port=18080, guest_port=8080)

        mock_stop_tunnel.assert_called_once_with(tunnel_proc)
        # nftables cleanup happens once immediately after failed probe in expose_local
        mock_sdk.network.cleanup_local_port_forward.assert_called_once()


class TestVMContextManager:
    """Tests for VM context manager."""

    @patch("smolvm.facade.SmolVMManager")
    def test_context_manager_stops_on_exit(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test that context manager stops VM on exit."""
        mock_sdk = MagicMock()
        running_info = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        stopped_info = MagicMock(vm_id="vm001", status=VMState.STOPPED)
        mock_sdk.create.return_value = running_info
        mock_sdk.stop.return_value = stopped_info
        mock_sdk_cls.return_value = mock_sdk

        with SmolVM(sample_config) as vm:
            assert vm.vm_id == "vm001"

        # stop/delete/close should have been called for owned VM
        mock_sdk.stop.assert_called_once()
        mock_sdk.delete.assert_called_once_with("vm001")
        mock_sdk.close.assert_called_once()

    @patch("smolvm.facade.SmolVMManager")
    def test_context_manager_autostarts_owned_vm(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test context manager auto-starts and then stops owned VMs."""
        mock_sdk = MagicMock()
        created_info = MagicMock(vm_id="vm001", status=VMState.CREATED)
        created_info.config.env_vars = {}
        running_info = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        running_info.config.env_vars = {}
        stopped_info = MagicMock(vm_id="vm001", status=VMState.STOPPED)

        mock_sdk.create.return_value = created_info
        mock_sdk.start.return_value = running_info
        mock_sdk.stop.return_value = stopped_info
        mock_sdk_cls.return_value = mock_sdk

        with SmolVM(sample_config):
            pass

        mock_sdk.start.assert_called_once_with("vm001", boot_timeout=30.0)
        mock_sdk.stop.assert_called_once()
        mock_sdk.delete.assert_called_once_with("vm001")
        mock_sdk.close.assert_called_once()

    @patch("smolvm.facade.SmolVMManager")
    def test_context_manager_from_id_does_not_delete(
        self,
        mock_sdk_cls: MagicMock,
    ) -> None:
        """Reconnect mode should not auto-delete existing VM on context exit."""
        running_info = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        stopped_info = MagicMock(vm_id="vm001", status=VMState.STOPPED)

        mock_sdk = MagicMock()
        mock_sdk.get.return_value = running_info
        mock_sdk.stop.return_value = stopped_info
        mock_sdk_cls.from_id.return_value = mock_sdk

        with SmolVM.from_id("vm001") as vm:
            assert vm.vm_id == "vm001"

        mock_sdk.stop.assert_called_once()
        mock_sdk.delete.assert_not_called()
        mock_sdk.close.assert_called_once()


class TestVMProperties:
    """Tests for VM properties."""

    @patch("smolvm.facade.SmolVMManager")
    def test_get_ip(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test getting the IP address."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.CREATED
        mock_info.network = mock_network

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = mock_info
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        assert vm.get_ip() == "172.16.0.2"

    @patch("smolvm.facade.SmolVMManager")
    def test_get_ip_no_network_raises(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test get_ip raises when no network config."""
        mock_info = MagicMock()
        mock_info.network = None

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        with pytest.raises(SmolVMError, match="no network"):
            vm.get_ip()

    @patch("smolvm.facade.SmolVMManager")
    def test_data_dir_property(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test exposing SDK data_dir through VM facade."""
        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.CREATED
        mock_info.network = None

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = mock_info
        mock_sdk.data_dir = Path("/tmp/smolvm-test")
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        assert vm.data_dir == Path("/tmp/smolvm-test")

    @patch("smolvm.facade.SmolVMManager")
    def test_repr(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test __repr__."""
        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        assert "vm001" in repr(vm)
        assert "created" in repr(vm)

    @patch("smolvm.facade.SmolVMManager")
    def test_ssh_commands_proxy(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test ssh_commands proxies through to SDK helper."""
        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get_ssh_commands.return_value = {
            "private_ip": "ssh root@172.16.0.2",
            "localhost_port": "ssh -p 2200 root@127.0.0.1",
        }
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config, ssh_key_path="/tmp/id_ed25519")
        cmds = vm.ssh_commands(public_host="203.0.113.10")

        assert "private_ip" in cmds
        mock_sdk.get_ssh_commands.assert_called_once_with(
            "vm001",
            ssh_user="root",
            key_path="/tmp/id_ed25519",
            public_host="203.0.113.10",
        )

    @patch("smolvm.facade.SmolVMManager")
    def test_ssh_attach_command_uses_resolved_client(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Interactive SSH command should reuse the resolved SSH client endpoint."""
        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        vm._ssh = MagicMock(
            host="172.16.0.2",
            port=22,
            user="root",
            key_path="/tmp/id_ed25519",
        )
        vm._ssh_ready = True

        assert vm._ssh_attach_command() == [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-p",
            "22",
            "-i",
            "/tmp/id_ed25519",
            "-o",
            "IdentitiesOnly=yes",
            "root@172.16.0.2",
        ]


class TestVMEnvInjection:
    """Tests for environment variable injection during start()."""

    @patch("smolvm.facade.inject_env_vars")
    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVMManager")
    def test_start_injects_env_vars(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        mock_inject: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test that start() injects env vars if configured."""
        mock_sdk = MagicMock()
        mock_info = MagicMock(vm_id="vm001", status=VMState.CREATED)
        # Add env vars to the runtime config (simulating start returning info)
        config_with_env = sample_config.model_copy(
            update={"env_vars": {"FOO": "bar"}, "boot_args": "init=/init"}
        )
        mock_info.config = config_with_env
        mock_info.network.guest_ip = "172.16.0.2"

        # Mock start() transitioning to RUNNING
        running_info = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        running_info.config = config_with_env
        running_info.network.guest_ip = "172.16.0.2"

        mock_sdk.create.return_value = mock_info
        mock_sdk.start.return_value = running_info
        # wait_for_ssh calls get() to poll status
        mock_sdk.get.return_value = running_info
        mock_sdk_cls.return_value = mock_sdk

        mock_ssh = MagicMock()
        mock_ssh_cls.return_value = mock_ssh
        mock_inject.return_value = ["FOO"]

        vm = SmolVM(config_with_env)
        vm.start()

        # Should wait for SSH
        mock_ssh.wait_for_ssh.assert_called_once()
        # Should create SSH client
        mock_ssh_cls.assert_called()
        # Should call inject
        mock_inject.assert_called_once_with(mock_ssh, {"FOO": "bar"})

    @patch("smolvm.facade.inject_env_vars")
    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVMManager")
    def test_start_injects_env_vars_with_ssh_fallback(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        mock_inject: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """start() should fallback to guest IP for env injection if localhost SSH fails."""
        mock_sdk = MagicMock()
        config_with_env = sample_config.model_copy(
            update={"env_vars": {"FOO": "bar"}, "boot_args": "init=/init"}
        )

        created_info = MagicMock(vm_id="vm001", status=VMState.CREATED)
        created_info.config = config_with_env
        created_info.network.guest_ip = "172.16.0.2"
        created_info.network.ssh_host_port = 2200

        running_info = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        running_info.config = config_with_env
        running_info.network.guest_ip = "172.16.0.2"
        running_info.network.ssh_host_port = 2200

        mock_sdk.create.return_value = created_info
        mock_sdk.start.return_value = running_info
        mock_sdk.get.return_value = running_info
        mock_sdk_cls.return_value = mock_sdk

        localhost_client = MagicMock()
        localhost_client.wait_for_ssh.side_effect = OperationTimeoutError("wait_for_ssh", 15.0)
        guest_client = MagicMock()
        mock_ssh_cls.side_effect = [localhost_client, guest_client]
        mock_inject.return_value = ["FOO"]

        vm = SmolVM(config_with_env)
        vm.start()

        assert mock_ssh_cls.call_count == 2
        assert mock_ssh_cls.call_args_list[0].kwargs["host"] == "127.0.0.1"
        assert mock_ssh_cls.call_args_list[0].kwargs["port"] == 2200
        assert mock_ssh_cls.call_args_list[1].kwargs["host"] == "172.16.0.2"
        assert mock_ssh_cls.call_args_list[1].kwargs["port"] == 22
        mock_inject.assert_called_once_with(guest_client, {"FOO": "bar"})

    @patch("smolvm.facade.inject_env_vars")
    @patch("smolvm.facade.SmolVMManager")
    def test_start_skips_injection_if_no_env_vars(
        self,
        mock_sdk_cls: MagicMock,
        mock_inject: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test that start() skips injection if env_vars is empty."""
        mock_sdk = MagicMock()
        mock_info = MagicMock(vm_id="vm001", status=VMState.CREATED)
        # Empty env_vars
        config = sample_config.model_copy(update={"env_vars": {}, "boot_args": "init=/init"})
        mock_info.config = config

        mock_sdk.create.return_value = mock_info
        mock_sdk.start.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(config)
        vm.start()

        mock_inject.assert_not_called()

    @patch("smolvm.facade.inject_env_vars")
    @patch("smolvm.facade.SmolVMManager")
    def test_start_raises_if_ssh_not_supported(
        self,
        mock_sdk_cls: MagicMock,
        mock_inject: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test that start() raises if env_vars set but no SSH support."""
        mock_sdk = MagicMock()
        mock_info = MagicMock(vm_id="vm001", status=VMState.CREATED)
        # boot_args missing init=/init
        config = sample_config.model_copy(
            update={"env_vars": {"FOO": "bar"}, "boot_args": "console=ttyS0"}
        )
        mock_info.config = config

        mock_sdk.create.return_value = mock_info
        mock_sdk.start.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(config)

        with pytest.raises(SmolVMError, match="does not support guest SSH"):
            vm.start()

        mock_inject.assert_not_called()


class TestVMEnvManagement:
    """Tests for runtime environment variable management methods."""

    @patch("smolvm.facade.inject_env_vars")
    @patch("smolvm.facade.SmolVMManager")
    def test_set_env_vars(
        self,
        mock_sdk_cls: MagicMock,
        mock_inject: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """set_env_vars should delegate to inject_env_vars with merge=True."""
        config = sample_config.model_copy(update={"boot_args": "init=/init"})
        running_info = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        running_info.config = config
        running_info.network.guest_ip = "172.16.0.2"
        running_info.network.ssh_host_port = None

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = running_info
        mock_sdk.get.return_value = running_info
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(config)
        vm._ssh = MagicMock()
        vm._ssh_ready = True
        mock_inject.return_value = ["FOO"]

        result = vm.set_env_vars({"FOO": "bar"})

        assert result == ["FOO"]
        mock_inject.assert_called_once_with(vm._ssh, {"FOO": "bar"}, merge=True)

    @patch("smolvm.facade.remove_env_vars")
    @patch("smolvm.facade.SmolVMManager")
    def test_unset_env_vars(
        self,
        mock_sdk_cls: MagicMock,
        mock_remove: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """unset_env_vars should delegate to remove_env_vars."""
        config = sample_config.model_copy(update={"boot_args": "init=/init"})
        running_info = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        running_info.config = config
        running_info.network.guest_ip = "172.16.0.2"
        running_info.network.ssh_host_port = None

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = running_info
        mock_sdk.get.return_value = running_info
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(config)
        vm._ssh = MagicMock()
        vm._ssh_ready = True
        mock_remove.return_value = {"FOO": "bar"}

        result = vm.unset_env_vars(["FOO"])

        assert result == {"FOO": "bar"}
        mock_remove.assert_called_once_with(vm._ssh, ["FOO"])

    @patch("smolvm.facade.read_env_vars")
    @patch("smolvm.facade.SmolVMManager")
    def test_list_env_vars(
        self,
        mock_sdk_cls: MagicMock,
        mock_read: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """list_env_vars should delegate to read_env_vars."""
        config = sample_config.model_copy(update={"boot_args": "init=/init"})
        running_info = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        running_info.config = config
        running_info.network.guest_ip = "172.16.0.2"
        running_info.network.ssh_host_port = None

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = running_info
        mock_sdk.get.return_value = running_info
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(config)
        vm._ssh = MagicMock()
        vm._ssh_ready = True
        mock_read.return_value = {"FOO": "bar"}

        result = vm.list_env_vars()

        assert result == {"FOO": "bar"}
        mock_read.assert_called_once_with(vm._ssh)

    @patch("smolvm.facade.SmolVMManager")
    def test_close_proxies_to_sdk(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """close() should release underlying SDK resources."""
        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(sample_config)
        vm.close()

        mock_sdk.close.assert_called_once()
