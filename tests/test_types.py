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

"""Tests for SmolVM types module."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from smolvm.types import CommandResult, NetworkConfig, VMConfig, VMInfo, VMState


class TestVMConfig:
    """Tests for VMConfig validation."""

    def test_valid_config(self, tmp_path: Path) -> None:
        """Test creating a valid VMConfig."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config = VMConfig(
            vm_id="vm001",
            vcpu_count=2,
            mem_size_mib=512,
            kernel_path=kernel,
            rootfs_path=rootfs,
        )

        assert config.vm_id == "vm001"
        assert config.vcpu_count == 2
        assert config.mem_size_mib == 512

    def test_vm_id_auto_generated_when_omitted(self, tmp_path: Path) -> None:
        """Test VM ID is generated when omitted."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config = VMConfig(
            kernel_path=kernel,
            rootfs_path=rootfs,
        )

        assert config.vm_id.startswith("vm-")
        assert re.fullmatch(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$", config.vm_id)

    def test_vm_id_auto_generated_when_none(self, tmp_path: Path) -> None:
        """Test VM ID is generated when explicitly set to None."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config = VMConfig(  # type: ignore[arg-type]
            vm_id=None,
            kernel_path=kernel,
            rootfs_path=rootfs,
        )

        assert config.vm_id.startswith("vm-")

    def test_invalid_vm_id_uppercase(self, tmp_path: Path) -> None:
        """Test that uppercase VM IDs are rejected."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        with pytest.raises(ValidationError) as exc_info:
            VMConfig(
                vm_id="VM001",
                kernel_path=kernel,
                rootfs_path=rootfs,
            )

        assert "vm_id" in str(exc_info.value)

    def test_invalid_vm_id_special_chars(self, tmp_path: Path) -> None:
        """Test that special characters in VM ID are rejected."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        with pytest.raises(ValidationError):
            VMConfig(
                vm_id="vm_001@test",
                kernel_path=kernel,
                rootfs_path=rootfs,
            )

    def test_vcpu_bounds(self, tmp_path: Path) -> None:
        """Test vCPU count bounds."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        # Too low
        with pytest.raises(ValidationError):
            VMConfig(
                vm_id="vm001",
                vcpu_count=0,
                kernel_path=kernel,
                rootfs_path=rootfs,
            )

        # Too high
        with pytest.raises(ValidationError):
            VMConfig(
                vm_id="vm001",
                vcpu_count=64,
                kernel_path=kernel,
                rootfs_path=rootfs,
            )

    def test_memory_bounds(self, tmp_path: Path) -> None:
        """Test memory size bounds."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        # Too low
        with pytest.raises(ValidationError):
            VMConfig(
                vm_id="vm001",
                mem_size_mib=64,
                kernel_path=kernel,
                rootfs_path=rootfs,
            )

        # Too high
        with pytest.raises(ValidationError):
            VMConfig(
                vm_id="vm001",
                mem_size_mib=32768,
                kernel_path=kernel,
                rootfs_path=rootfs,
            )

    def test_path_validation_missing_kernel(self, tmp_path: Path) -> None:
        """Test that missing kernel path is rejected."""
        rootfs = tmp_path / "rootfs.ext4"
        rootfs.touch()

        with pytest.raises(ValidationError) as exc_info:
            VMConfig(
                vm_id="vm001",
                kernel_path=tmp_path / "nonexistent",
                rootfs_path=rootfs,
            )

        assert "does not exist" in str(exc_info.value)

    def test_path_validation_directory_rejected(self, tmp_path: Path) -> None:
        """Test that directory paths are rejected (must be files)."""
        rootfs = tmp_path / "rootfs.ext4"
        rootfs.touch()

        with pytest.raises(ValidationError) as exc_info:
            VMConfig(
                vm_id="vm001",
                kernel_path=tmp_path,  # This is a directory
                rootfs_path=rootfs,
            )

        assert "not a file" in str(exc_info.value)

    def test_env_vars_valid(self, tmp_path: Path) -> None:
        """Test that valid env_vars are accepted."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config = VMConfig(
            vm_id="vm001",
            kernel_path=kernel,
            rootfs_path=rootfs,
            env_vars={"FOO": "bar", "BAZ_2": "qux", "_PRIVATE": "val"},
        )
        assert config.env_vars == {"FOO": "bar", "BAZ_2": "qux", "_PRIVATE": "val"}

    def test_env_vars_default_empty(self, tmp_path: Path) -> None:
        """Test that env_vars defaults to empty dict."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config = VMConfig(
            vm_id="vm001",
            kernel_path=kernel,
            rootfs_path=rootfs,
        )
        assert config.env_vars == {}

    def test_env_vars_invalid_key_empty(self, tmp_path: Path) -> None:
        """Test that empty env var key is rejected."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        with pytest.raises(ValidationError, match="cannot be empty"):
            VMConfig(
                vm_id="vm001",
                kernel_path=kernel,
                rootfs_path=rootfs,
                env_vars={"": "value"},
            )

    def test_env_vars_invalid_key_starts_with_digit(self, tmp_path: Path) -> None:
        """Test that env var key starting with digit is rejected."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        with pytest.raises(ValidationError, match="Invalid"):
            VMConfig(
                vm_id="vm001",
                kernel_path=kernel,
                rootfs_path=rootfs,
                env_vars={"123KEY": "value"},
            )

    def test_env_vars_invalid_key_with_equals(self, tmp_path: Path) -> None:
        """Test that env var key containing = is rejected."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        with pytest.raises(ValidationError, match="Invalid"):
            VMConfig(
                vm_id="vm001",
                kernel_path=kernel,
                rootfs_path=rootfs,
                env_vars={"A=B": "value"},
            )

    def test_disk_mode_defaults_to_isolated(self, tmp_path: Path) -> None:
        """Test default disk mode is isolated for sandbox-by-default behavior."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config = VMConfig(vm_id="vm001", kernel_path=kernel, rootfs_path=rootfs)

        assert config.disk_mode == "isolated"
        assert config.retain_disk_on_delete is False

    def test_invalid_disk_mode_rejected(self, tmp_path: Path) -> None:
        """Test unsupported disk_mode values are rejected."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        with pytest.raises(ValidationError):
            VMConfig(  # type: ignore[arg-type]
                vm_id="vm001",
                kernel_path=kernel,
                rootfs_path=rootfs,
                disk_mode="snapshot",
            )


class TestVMState:
    """Tests for VMState enum."""

    def test_state_values(self) -> None:
        """Test all state values exist."""
        assert VMState.CREATED.value == "created"
        assert VMState.RUNNING.value == "running"
        assert VMState.STOPPED.value == "stopped"
        assert VMState.ERROR.value == "error"


class TestNetworkConfig:
    """Tests for NetworkConfig."""

    def test_network_config_creation(self) -> None:
        """Test creating a NetworkConfig."""
        config = NetworkConfig(
            guest_ip="172.16.0.2",
            tap_device="tap1",
            guest_mac="AA:FC:00:00:00:01",
        )

        assert config.guest_ip == "172.16.0.2"
        assert config.gateway_ip == "172.16.0.1"  # Default
        assert config.tap_device == "tap1"

    def test_network_config_immutable(self) -> None:
        """Test that NetworkConfig is immutable (frozen)."""
        config = NetworkConfig(
            guest_ip="172.16.0.2",
            tap_device="tap1",
            guest_mac="AA:FC:00:00:00:01",
        )

        with pytest.raises(ValidationError):
            config.guest_ip = "172.16.0.3"  # type: ignore


class TestVMInfo:
    """Tests for VMInfo."""

    def test_vm_info_creation(self, tmp_path: Path) -> None:
        """Test creating VMInfo."""
        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config = VMConfig(
            vm_id="vm001",
            kernel_path=kernel,
            rootfs_path=rootfs,
        )

        info = VMInfo(
            vm_id="vm001",
            status=VMState.CREATED,
            config=config,
        )

        assert info.vm_id == "vm001"
        assert info.status == VMState.CREATED
        assert info.network is None
        assert info.pid is None


class TestCommandResult:
    """Tests for CommandResult helpers."""

    def test_ok_property(self) -> None:
        """Test success helper reflects exit code."""
        success = CommandResult(exit_code=0, stdout="ok\n", stderr="")
        failure = CommandResult(exit_code=1, stdout="", stderr="boom")

        assert success.ok is True
        assert failure.ok is False

    def test_output_property_strips_stdout(self) -> None:
        """Test output convenience helper returns stripped stdout."""
        result = CommandResult(
            exit_code=0,
            stdout="Hello from the sandbox!\n",
            stderr="",
        )

        assert result.output == "Hello from the sandbox!"
