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

from smolvm.exceptions import (
    CommandExecutionUnavailableError,
    OperationTimeoutError,
    SmolVMError,
)
from smolvm.facade import SmolVM
from smolvm.types import VMConfig, VMState


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
    def test_neither_config_nor_id_autoconfigures(
        self,
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
    def test_autoconfigure_with_custom_mem_and_disk(
        self,
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

    def test_custom_auto_sizing_with_config_raises(self, sample_config: VMConfig) -> None:
        """Custom auto sizing options are only valid in zero-config mode."""
        with pytest.raises(ValueError, match="auto-config mode"):
            SmolVM(sample_config, mem_size_mib=1024)

    @patch("smolvm.facade.SmolVMManager")
    def test_from_id(self, mock_sdk_cls: MagicMock) -> None:
        """Test reconnecting to an existing VM by ID."""
        mock_sdk = MagicMock()
        mock_sdk.get.return_value = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        mock_sdk_cls.from_id.return_value = mock_sdk

        vm = SmolVM.from_id("vm001")

        assert vm.vm_id == "vm001"
        mock_sdk_cls.from_id.assert_called_once()


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

    @patch("smolvm.utils.ensure_ssh_key")
    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVMManager")
    def test_run_retries_with_default_key_when_no_explicit_key(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        mock_ensure_ssh_key: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """run() should retry with SmolVM default key when implicit auth fails."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"
        mock_network.ssh_host_port = None

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network
        mock_info.config.boot_args = "console=ttyS0 reboot=k panic=1 pci=off init=/init"

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        private_key = Path("/tmp/id_ed25519")
        public_key = Path("/tmp/id_ed25519.pub")
        mock_ensure_ssh_key.return_value = (private_key, public_key)

        no_key_client = MagicMock()
        no_key_client.wait_for_ssh.side_effect = OperationTimeoutError("wait_for_ssh", 10.0)

        key_client = MagicMock()
        key_client.run.return_value = MagicMock(exit_code=0, stdout="ok\n", stderr="")

        mock_ssh_cls.side_effect = [no_key_client, key_client]

        vm = SmolVM(sample_config)
        result = vm.run("echo ok")

        assert result.exit_code == 0
        assert mock_ssh_cls.call_count == 2
        assert mock_ssh_cls.call_args_list[0].kwargs["key_path"] is None
        assert mock_ssh_cls.call_args_list[1].kwargs["key_path"] == str(private_key)
        assert vm._ssh_key_path == str(private_key)
        mock_ensure_ssh_key.assert_called_once()

    @patch("smolvm.facade.Path.home")
    @patch("smolvm.utils.ensure_ssh_key")
    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVMManager")
    def test_run_retries_with_legacy_default_key_when_key_resolution_fails(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        mock_ensure_ssh_key: MagicMock,
        mock_home: MagicMock,
        sample_config: VMConfig,
        tmp_path: Path,
    ) -> None:
        """run() should fallback to legacy key path when ensure_ssh_key fails."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"
        mock_network.ssh_host_port = None

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network
        mock_info.config.boot_args = "console=ttyS0 reboot=k panic=1 pci=off init=/init"

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        mock_home.return_value = tmp_path
        legacy_dir = tmp_path / ".smolvm"
        legacy_dir.mkdir(parents=True)
        legacy_private = legacy_dir / "id_ed25519"
        legacy_private.write_text("legacy-private")

        mock_ensure_ssh_key.side_effect = PermissionError("permission denied")

        no_key_client = MagicMock()
        no_key_client.wait_for_ssh.side_effect = OperationTimeoutError("wait_for_ssh", 10.0)

        key_client = MagicMock()
        key_client.run.return_value = MagicMock(exit_code=0, stdout="ok\n", stderr="")

        mock_ssh_cls.side_effect = [no_key_client, key_client]

        vm = SmolVM(sample_config)
        result = vm.run("echo ok")

        assert result.exit_code == 0
        assert mock_ssh_cls.call_count == 2
        assert mock_ssh_cls.call_args_list[0].kwargs["key_path"] is None
        assert mock_ssh_cls.call_args_list[1].kwargs["key_path"] == str(legacy_private)
        assert vm._ssh_key_path == str(legacy_private)
        mock_ensure_ssh_key.assert_called_once()

    @patch("smolvm.utils.ensure_ssh_key")
    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVMManager")
    def test_run_with_explicit_key_does_not_resolve_default_key(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        mock_ensure_ssh_key: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """run() should not resolve fallback key when an explicit key is set."""
        mock_network = MagicMock()
        mock_network.guest_ip = "172.16.0.2"
        mock_network.ssh_host_port = None

        mock_info = MagicMock()
        mock_info.vm_id = "vm001"
        mock_info.status = VMState.RUNNING
        mock_info.network = mock_network
        mock_info.config.boot_args = "console=ttyS0 reboot=k panic=1 pci=off init=/init"

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.get.return_value = mock_info
        mock_sdk_cls.return_value = mock_sdk

        timeout_client = MagicMock()
        timeout_client.wait_for_ssh.side_effect = OperationTimeoutError("wait_for_ssh", 10.0)
        mock_ssh_cls.return_value = timeout_client

        vm = SmolVM(sample_config, ssh_key_path="/custom/id_ed25519")
        with pytest.raises(CommandExecutionUnavailableError, match="SSH did not become ready"):
            vm.run("echo ok")

        mock_ensure_ssh_key.assert_not_called()
        mock_ssh_cls.assert_called_once()
        assert mock_ssh_cls.call_args.kwargs["key_path"] == "/custom/id_ed25519"

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
        with pytest.raises(CommandExecutionUnavailableError, match="init=/init"):
            vm.run("echo test")

        mock_ssh_cls.assert_not_called()

    @patch("smolvm.utils.ensure_ssh_key")
    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVMManager")
    def test_run_maps_ssh_readiness_timeout_to_clear_error(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        mock_ensure_ssh_key: MagicMock,
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
        mock_ensure_ssh_key.return_value = (Path("/tmp/id_ed25519"), Path("/tmp/id_ed25519.pub"))

        vm = SmolVM(sample_config)
        with pytest.raises(CommandExecutionUnavailableError, match="SSH did not become ready"):
            vm.run("echo test")

        mock_ensure_ssh_key.assert_called_once()
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


class TestVMLocalExpose:
    """Tests for localhost-only port exposure."""

    @patch("smolvm.facade.SmolVMManager")
    @patch("smolvm.facade.SmolVM._probe_local_forward", return_value=True)
    def test_expose_local_with_explicit_host_port(
        self,
        _mock_probe: MagicMock,
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
    @patch("smolvm.facade.SmolVM._probe_local_forward", return_value=True)
    def test_stop_cleans_local_forwards(
        self,
        _mock_probe: MagicMock,
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
        """Falls back to SSH tunnel when iptables local path is unreachable."""
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
        # iptables cleanup happens once immediately after failed probe in expose_local
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

        with pytest.raises(SmolVMError, match="does not support SSH"):
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
