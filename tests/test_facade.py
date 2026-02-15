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
from smolvm.exceptions import CommandExecutionUnavailableError, SmolVMError
from smolvm.facade import VM
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

    @patch("smolvm.facade.SmolVM")
    def test_create_with_config(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test creating a VM with a config."""
        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk_cls.return_value = mock_sdk

        vm = VM(sample_config)

        assert vm.vm_id == "vm001"
        mock_sdk.create.assert_called_once_with(sample_config)

    def test_both_config_and_id_raises(self, sample_config: VMConfig) -> None:
        """Test that passing both config and vm_id raises ValueError."""
        with pytest.raises(ValueError, match="not both"):
            VM(sample_config, vm_id="vm001")

    @patch("smolvm.facade.SmolVM")
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

        vm = VM()

        assert vm.vm_id.startswith("vm-")
        mock_builder.build_alpine_ssh_key.assert_called_once()
        assert mock_builder.build_alpine_ssh_key.call_args.args[0] == public_key
        mock_sdk.create.assert_called_once()
        created_config = mock_sdk.create.call_args[0][0]
        assert "init=/init" in created_config.boot_args

    @patch("smolvm.facade.SmolVM")
    def test_from_id(self, mock_sdk_cls: MagicMock) -> None:
        """Test reconnecting to an existing VM by ID."""
        mock_sdk = MagicMock()
        mock_sdk.get.return_value = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        mock_sdk_cls.from_id.return_value = mock_sdk

        vm = VM.from_id("vm001")

        assert vm.vm_id == "vm001"
        mock_sdk_cls.from_id.assert_called_once()


class TestVMLifecycle:
    """Tests for VM lifecycle operations."""

    @patch("smolvm.facade.SmolVM")
    def test_start_returns_self(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test that start() returns self for chaining."""
        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk.start.return_value = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        mock_sdk_cls.return_value = mock_sdk

        vm = VM(sample_config)
        result = vm.start()

        assert result is vm
        mock_sdk.start.assert_called_once()

    @patch("smolvm.facade.SmolVM")
    def test_start_noop_if_already_running(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test start() is a no-op when VM is already running."""
        mock_sdk = MagicMock()
        running_info = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        mock_sdk.create.return_value = running_info
        mock_sdk_cls.return_value = mock_sdk

        vm = VM(sample_config)
        result = vm.start()

        assert result is vm
        mock_sdk.start.assert_not_called()

    @patch("smolvm.facade.SmolVM")
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

        vm = VM(sample_config)
        result = vm.stop()

        assert result is vm
        mock_sdk.stop.assert_called_once()

    @patch("smolvm.facade.SmolVM")
    def test_delete(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test deleting a VM."""
        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk_cls.return_value = mock_sdk

        vm = VM(sample_config)
        vm.delete()

        mock_sdk.delete.assert_called_once_with("vm001")


class TestVMRun:
    """Tests for command execution on the VM."""

    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVM")
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

        vm = VM(sample_config)
        result = vm.run("echo ok")

        assert result.exit_code == 0
        mock_ssh.wait_for_ssh.assert_called_once_with(timeout=30.0)
        mock_ssh.run.assert_called_once_with("echo ok", timeout=30)

    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVM")
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

        vm = VM(sample_config)
        vm.run("echo one")
        vm.run("echo two")

        mock_ssh.wait_for_ssh.assert_called_once_with(timeout=30.0)
        assert mock_ssh.run.call_count == 2

    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVM")
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

        vm = VM(sample_config)
        with pytest.raises(CommandExecutionUnavailableError, match="init=/init"):
            vm.run("echo test")

        mock_ssh_cls.assert_not_called()

    @patch("smolvm.facade.SSHClient")
    @patch("smolvm.facade.SmolVM")
    def test_run_maps_ssh_readiness_timeout_to_clear_error(
        self,
        mock_sdk_cls: MagicMock,
        mock_ssh_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test run() surfaces readiness timeout as command-unavailable error."""
        from smolvm.exceptions import OperationTimeoutError

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

        vm = VM(sample_config)
        with pytest.raises(CommandExecutionUnavailableError, match="SSH did not become ready"):
            vm.run("echo test")

        mock_ssh.run.assert_not_called()

    @patch("smolvm.facade.SmolVM")
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

        vm = VM(sample_config)
        with pytest.raises(SmolVMError, match="VM is stopped"):
            vm.run("echo test")


class TestVMLocalExpose:
    """Tests for localhost-only port exposure."""

    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.facade.VM._probe_local_forward", return_value=True)
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

        vm = VM(sample_config)
        host_port = vm.expose_local(guest_port=8080, host_port=18080)

        assert host_port == 18080
        mock_sdk.network.setup_local_port_forward.assert_called_once_with(
            vm_id="vm001",
            guest_ip="172.16.0.2",
            host_port=18080,
            guest_port=8080,
        )

    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.facade.VM._probe_local_forward", return_value=True)
    @patch("smolvm.facade.VM._find_available_local_port", side_effect=[18081, 18082])
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

        vm = VM(sample_config)
        host_port = vm.expose_local(guest_port=8080)

        assert host_port == 18081
        assert mock_find_port.call_count == 2
        mock_sdk.network.setup_local_port_forward.assert_called_once()

    @patch("smolvm.facade.SmolVM")
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

        vm = VM(sample_config)
        with pytest.raises(SmolVMError, match="VM is stopped"):
            vm.expose_local(guest_port=8080, host_port=18080)

    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.facade.VM._probe_local_forward", return_value=True)
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

        vm = VM(sample_config)
        vm.expose_local(guest_port=8080, host_port=18080)
        vm.stop()

        mock_sdk.network.cleanup_local_port_forward.assert_called_once_with(
            vm_id="vm001",
            guest_ip="172.16.0.2",
            host_port=18080,
            guest_port=8080,
        )

    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.facade.VM._start_local_tunnel")
    @patch("smolvm.facade.VM._probe_local_forward", return_value=False)
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

        vm = VM(sample_config)
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

    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.facade.VM._allocate_local_port", return_value=18081)
    @patch("smolvm.facade.VM._start_local_tunnel")
    @patch("smolvm.facade.VM._probe_local_forward", return_value=False)
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

        vm = VM(sample_config)
        host_port = vm.expose_local(guest_port=8080, host_port=18080)

        assert host_port == 18081
        assert mock_start_tunnel.call_count == 2
        first_call = mock_start_tunnel.call_args_list[0]
        second_call = mock_start_tunnel.call_args_list[1]
        assert first_call.kwargs == {"host_port": 18080, "guest_port": 8080}
        assert second_call.kwargs == {"host_port": 18081, "guest_port": 8080}

    @patch("smolvm.facade.SmolVM")
    @patch("smolvm.facade.VM._stop_local_tunnel")
    @patch("smolvm.facade.VM._start_local_tunnel")
    @patch("smolvm.facade.VM._probe_local_forward", return_value=False)
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

        vm = VM(sample_config)
        vm.expose_local(guest_port=8080, host_port=18080)
        vm.unexpose_local(host_port=18080, guest_port=8080)

        mock_stop_tunnel.assert_called_once_with(tunnel_proc)
        # iptables cleanup happens once immediately after failed probe in expose_local
        mock_sdk.network.cleanup_local_port_forward.assert_called_once()


class TestVMContextManager:
    """Tests for VM context manager."""

    @patch("smolvm.facade.SmolVM")
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

        with VM(sample_config) as vm:
            assert vm.vm_id == "vm001"

        # stop/delete/close should have been called for owned VM
        mock_sdk.stop.assert_called_once()
        mock_sdk.delete.assert_called_once_with("vm001")
        mock_sdk.close.assert_called_once()

    @patch("smolvm.facade.SmolVM")
    def test_context_manager_autostarts_owned_vm(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test context manager auto-starts and then stops owned VMs."""
        mock_sdk = MagicMock()
        created_info = MagicMock(vm_id="vm001", status=VMState.CREATED)
        running_info = MagicMock(vm_id="vm001", status=VMState.RUNNING)
        stopped_info = MagicMock(vm_id="vm001", status=VMState.STOPPED)
        mock_sdk.create.return_value = created_info
        mock_sdk.start.return_value = running_info
        mock_sdk.stop.return_value = stopped_info
        mock_sdk_cls.return_value = mock_sdk

        with VM(sample_config):
            pass

        mock_sdk.start.assert_called_once_with("vm001", boot_timeout=30.0)
        mock_sdk.stop.assert_called_once()
        mock_sdk.delete.assert_called_once_with("vm001")
        mock_sdk.close.assert_called_once()

    @patch("smolvm.facade.SmolVM")
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

        with VM.from_id("vm001") as vm:
            assert vm.vm_id == "vm001"

        mock_sdk.stop.assert_called_once()
        mock_sdk.delete.assert_not_called()
        mock_sdk.close.assert_called_once()


class TestVMProperties:
    """Tests for VM properties."""

    @patch("smolvm.facade.SmolVM")
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

        vm = VM(sample_config)
        assert vm.get_ip() == "172.16.0.2"

    @patch("smolvm.facade.SmolVM")
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

        vm = VM(sample_config)
        with pytest.raises(SmolVMError, match="no network"):
            vm.get_ip()

    @patch("smolvm.facade.SmolVM")
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

        vm = VM(sample_config)
        assert vm.data_dir == Path("/tmp/smolvm-test")

    @patch("smolvm.facade.SmolVM")
    def test_repr(
        self,
        mock_sdk_cls: MagicMock,
        sample_config: VMConfig,
    ) -> None:
        """Test __repr__."""
        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(vm_id="vm001", status=VMState.CREATED)
        mock_sdk_cls.return_value = mock_sdk

        vm = VM(sample_config)
        assert "vm001" in repr(vm)
        assert "created" in repr(vm)

    @patch("smolvm.facade.SmolVM")
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

        vm = VM(sample_config, ssh_key_path="/tmp/id_ed25519")
        cmds = vm.ssh_commands(public_host="203.0.113.10")

        assert "private_ip" in cmds
        mock_sdk.get_ssh_commands.assert_called_once_with(
            "vm001",
            ssh_user="root",
            key_path="/tmp/id_ed25519",
            public_host="203.0.113.10",
        )
