"""Unit tests for the QEMU runtime adapter."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.runtime.base import RuntimeContext
from smolvm.runtime.qemu import QemuRuntimeAdapter
from smolvm.types import NetworkConfig, VMConfig, VMInfo, VMState


def _make_context() -> RuntimeContext:
    """Build a minimal runtime context with mockable process hooks."""
    return RuntimeContext(
        data_dir=Path("/tmp/data"),
        socket_dir=Path("/tmp"),
        firmware_dir=Path("/tmp/data/firmware"),
        log_files={},
        process_handles={},
        resolve_boot_args=lambda vm_info: vm_info.config.boot_args,
        start_firecracker=MagicMock(),
        start_qemu=MagicMock(),
        unlink_socket=MagicMock(),
        kill_process=MagicMock(),
        wait_for_process=MagicMock(),
        is_process_running=MagicMock(),
        find_qemu_binary=MagicMock(),
    )


def _make_vm_info(tmp_path: Path, *, pid: int = 12345) -> VMInfo:
    """Create a minimal QEMU-backed VMInfo for adapter tests."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    socket_path = tmp_path / "qmp.sock"
    kernel.touch()
    rootfs.touch()
    socket_path.touch()

    return VMInfo(
        vm_id="vm-qemu-stop",
        status=VMState.RUNNING,
        config=VMConfig(
            vm_id="vm-qemu-stop",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
            boot_args="console=ttyAMA0 reboot=k panic=1 init=/init",
        ),
        network=NetworkConfig(
            guest_ip="10.0.2.15",
            gateway_ip="10.0.2.2",
            netmask="255.255.255.0",
            tap_device="usernet",
            guest_mac="aa:fc:00:00:00:01",
            ssh_host_port=2200,
        ),
        pid=pid,
        control_socket_path=socket_path,
    )


def test_stop_waits_for_hard_kill_before_releasing_socket(tmp_path: Path) -> None:
    """Forced QEMU termination should wait for exit before cleanup proceeds."""
    context = _make_context()
    context.is_process_running.side_effect = [True, True, False]
    adapter = QemuRuntimeAdapter(context)
    vm_info = _make_vm_info(tmp_path)

    with patch("os.kill") as mock_os_kill:
        adapter.stop(vm_info, timeout=10.0)

    mock_os_kill.assert_called_once()
    context.kill_process.assert_called_once_with(vm_info.pid)
    assert context.wait_for_process.call_args_list == [
        call(vm_info.pid, 10.0),
        call(vm_info.pid, 5.0),
    ]
    context.unlink_socket.assert_called_once_with(vm_info.control_socket_path)


def test_stop_raises_when_qemu_survives_hard_kill(tmp_path: Path) -> None:
    """Cleanup should fail loudly if the QEMU process still has not exited."""
    context = _make_context()
    context.is_process_running.side_effect = [True, True, True]
    adapter = QemuRuntimeAdapter(context)
    vm_info = _make_vm_info(tmp_path)

    with patch("os.kill") as mock_os_kill, pytest.raises(SmolVMError, match="did not exit"):
        adapter.stop(vm_info, timeout=10.0)

    mock_os_kill.assert_called_once()
    context.kill_process.assert_called_once_with(vm_info.pid)
    assert context.wait_for_process.call_args_list == [
        call(vm_info.pid, 10.0),
        call(vm_info.pid, 5.0),
    ]
    context.unlink_socket.assert_not_called()


def test_qcow2_backing_inspection_force_shares_running_qemu_disk(tmp_path: Path) -> None:
    """Inspecting a paused-but-open QEMU disk must bypass qemu-img's image lock."""
    disk = tmp_path / "vm.qcow2"
    disk.touch()
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='{"full-backing-filename": "/tmp/base.qcow2"}',
        stderr="",
    )

    with (
        patch("smolvm.runtime.qemu.which", return_value=Path("/usr/bin/qemu-img")),
        patch("smolvm.runtime.qemu.subprocess.run", return_value=result) as mock_run,
    ):
        backing = QemuRuntimeAdapter._qcow2_backing_file_required(disk)

    assert backing == Path("/tmp/base.qcow2")
    mock_run.assert_called_once_with(
        ["/usr/bin/qemu-img", "info", "-U", "--output=json", str(disk)],
        capture_output=True,
        text=True,
        check=False,
    )
