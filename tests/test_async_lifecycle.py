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

"""Tests for async VM lifecycle methods."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.types import VMConfig, VMState

# ---------------------------------------------------------------------------
# async_run_command
# ---------------------------------------------------------------------------


class TestAsyncRunCommand:
    """Tests for the async subprocess wrapper."""

    @pytest.mark.asyncio
    async def test_async_run_command_success(self) -> None:
        """Successful command returns CompletedProcess."""
        from smolvm.utils import async_run_command

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"hello\n", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await async_run_command(["echo", "hello"], use_sudo=False)

        assert result.returncode == 0
        assert result.stdout == "hello\n"

    @pytest.mark.asyncio
    async def test_async_run_command_failure_raises(self) -> None:
        """Non-zero exit raises SmolVMError."""
        from smolvm.utils import async_run_command

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"error msg")
        mock_proc.returncode = 1

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            pytest.raises(SmolVMError, match="Command failed"),
        ):
            await async_run_command(["false"], use_sudo=False)

    @pytest.mark.asyncio
    async def test_async_run_command_timeout_raises(self) -> None:
        """Command that exceeds timeout raises SmolVMError."""
        from smolvm.utils import async_run_command

        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = asyncio.TimeoutError()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            pytest.raises(SmolVMError, match="timed out"),
        ):
            await async_run_command(["sleep", "100"], use_sudo=False, timeout=1)

    @pytest.mark.asyncio
    async def test_async_run_command_empty_raises(self) -> None:
        """Empty command raises ValueError."""
        from smolvm.utils import async_run_command

        with pytest.raises(ValueError, match="cmd cannot be empty"):
            await async_run_command([], use_sudo=False)


# ---------------------------------------------------------------------------
# Async SmolVMManager
# ---------------------------------------------------------------------------


class TestAsyncSmolVMManager:
    """Tests for async lifecycle methods on SmolVMManager."""

    @pytest.mark.asyncio
    async def test_async_create_resizes_and_grows_raw_qemu_disk(
        self,
        tmp_path: Path,
    ) -> None:
        """async_create should apply the same raw resize/grow path as create."""
        from smolvm.vm import SmolVMManager

        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.img"
        kernel.touch()
        rootfs.write_bytes(b"\0" * (1024 * 1024))
        config = VMConfig(
            vm_id="vm-async-create",
            kernel_path=kernel,
            rootfs_path=rootfs,
            rootfs_format="raw-ext4",
            backend="qemu",
            disk_size_mib=2,
            grow_filesystem=True,
        )
        manager = SmolVMManager(
            data_dir=tmp_path / "data-async-create",
            socket_dir=tmp_path / "sockets-async-create",
            backend="qemu",
        )

        async def _copy(source: Path, target: Path) -> None:
            target.write_bytes(source.read_bytes())

        with (
            patch.object(SmolVMManager, "_async_copy_with_reflink", side_effect=_copy),
            patch.object(manager, "_grow_raw_ext4_filesystem") as mock_grow,
        ):
            vm_info = await manager.async_create(config)

        expected_disk = manager.data_dir / "disks" / "vm-async-create.ext4"
        assert vm_info.config.rootfs_path == expected_disk
        assert vm_info.config.rootfs_format == "raw-ext4"
        assert expected_disk.stat().st_size == 2 * 1024 * 1024
        mock_grow.assert_called_once_with(expected_disk, "vm-async-create")

    @pytest.mark.asyncio
    @patch("smolvm.vm.SmolVMManager._runtime_adapter_for_backend")
    @patch("smolvm.vm.SmolVMManager._backend_for_vm")
    async def test_async_start(
        self,
        mock_backend_for_vm: MagicMock,
        mock_adapter_for_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """async_start should call adapter.async_start and update state."""
        from smolvm.runtime.base import RuntimeLaunch
        from smolvm.vm import SmolVMManager

        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config = VMConfig(
            vm_id="vm-async1",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
        )

        manager = SmolVMManager(data_dir=tmp_path / "data")
        manager.state.create_vm(config)
        # Manually set network so start() doesn't complain
        from smolvm.types import NetworkConfig

        manager.state.reserve_ssh_port("vm-async1")
        manager.state.update_vm(
            "vm-async1",
            network=NetworkConfig(
                guest_ip="10.0.2.15",
                gateway_ip="10.0.2.2",
                tap_device="usernet",
                guest_mac="52:54:00:00:00:01",
                ssh_host_port=2200,
            ),
        )

        mock_backend_for_vm.return_value = "qemu"
        mock_adapter = MagicMock()
        mock_adapter.async_start = AsyncMock(
            return_value=RuntimeLaunch(
                pid=12345,
                control_socket_path=tmp_path / "qmp.sock",
                status=VMState.RUNNING,
            )
        )
        mock_adapter_for_backend.return_value = mock_adapter

        result = await manager.async_start("vm-async1")

        assert result.status == VMState.RUNNING
        assert result.pid == 12345
        mock_adapter.async_start.assert_called_once()

    @pytest.mark.asyncio
    @patch("smolvm.vm.SmolVMManager._runtime_adapter_for_backend")
    @patch("smolvm.vm.SmolVMManager._backend_for_vm")
    async def test_async_stop(
        self,
        mock_backend_for_vm: MagicMock,
        mock_adapter_for_backend: MagicMock,
        tmp_path: Path,
    ) -> None:
        """async_stop should call adapter.async_stop and update state."""
        from smolvm.vm import SmolVMManager

        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config = VMConfig(
            vm_id="vm-async2",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
        )

        manager = SmolVMManager(data_dir=tmp_path / "data")
        manager.state.create_vm(config)
        manager.state.update_vm("vm-async2", status=VMState.RUNNING, pid=99999)

        mock_backend_for_vm.return_value = "qemu"
        mock_adapter = MagicMock()
        mock_adapter.async_stop = AsyncMock()
        mock_adapter_for_backend.return_value = mock_adapter

        result = await manager.async_stop("vm-async2")

        assert result.status == VMState.STOPPED
        mock_adapter.async_stop.assert_called_once()


# ---------------------------------------------------------------------------
# Async SmolVM facade
# ---------------------------------------------------------------------------


class TestAsyncSmolVMFacade:
    """Tests for async facade methods."""

    @pytest.mark.asyncio
    @patch("smolvm.facade.SmolVMManager")
    async def test_async_start_calls_sdk(
        self,
        mock_sdk_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """async_start should call sdk.async_start."""
        from smolvm.facade import SmolVM

        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config = VMConfig(
            vm_id="vm-facade1",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
        )

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(
            vm_id="vm-facade1",
            status=VMState.CREATED,
            config=config,
        )
        mock_sdk.async_start = AsyncMock(
            return_value=MagicMock(
                vm_id="vm-facade1",
                status=VMState.RUNNING,
                config=config,
                network=None,
            )
        )
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(config)
        result = await vm.async_start()

        assert result is vm
        mock_sdk.async_start.assert_called_once()

    @pytest.mark.asyncio
    @patch("smolvm.facade.SmolVMManager")
    async def test_async_stop_calls_sdk(
        self,
        mock_sdk_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """async_stop should call sdk.async_stop."""
        from smolvm.facade import SmolVM

        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config = VMConfig(
            vm_id="vm-facade2",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
        )

        mock_sdk = MagicMock()
        mock_info = MagicMock(
            vm_id="vm-facade2", status=VMState.RUNNING, config=config
        )
        mock_sdk.create.return_value = mock_info
        mock_sdk.async_stop = AsyncMock(
            return_value=MagicMock(
                vm_id="vm-facade2",
                status=VMState.STOPPED,
                config=config,
            )
        )
        mock_sdk_cls.return_value = mock_sdk

        vm = SmolVM(config)
        result = await vm.async_stop()

        assert result is vm
        mock_sdk.async_stop.assert_called_once()

    @pytest.mark.asyncio
    @patch("smolvm.facade.SmolVMManager")
    async def test_async_context_manager(
        self,
        mock_sdk_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Async context manager should start and stop/delete the VM."""
        from smolvm.facade import SmolVM

        kernel = tmp_path / "vmlinux"
        rootfs = tmp_path / "rootfs.ext4"
        kernel.touch()
        rootfs.touch()

        config = VMConfig(
            vm_id="vm-ctx",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
        )

        mock_sdk = MagicMock()
        mock_sdk.create.return_value = MagicMock(
            vm_id="vm-ctx", status=VMState.CREATED, config=config
        )
        mock_sdk.async_start = AsyncMock(
            return_value=MagicMock(
                vm_id="vm-ctx", status=VMState.RUNNING, config=config, network=None
            )
        )
        mock_sdk.async_stop = AsyncMock(
            return_value=MagicMock(
                vm_id="vm-ctx", status=VMState.STOPPED, config=config
            )
        )
        mock_sdk.async_delete = AsyncMock()
        mock_sdk_cls.return_value = mock_sdk

        async with SmolVM(config) as vm:
            assert vm.vm_id == "vm-ctx"
            mock_sdk.async_start.assert_called_once()

        mock_sdk.async_stop.assert_called_once()
        mock_sdk.async_delete.assert_called_once()
