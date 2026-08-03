from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from smolvm.storage import MemoryStateManager
from smolvm.types import VMConfig, VMState


def _manager_with_vm(
    tmp_path: Path,
    *,
    status: VMState = VMState.RUNNING,
    pid: int | None = None,
) -> MemoryStateManager:
    """Build a state manager holding one VM in the given status/pid."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()
    state = MemoryStateManager(tmp_path)
    state.create_vm(VMConfig(vm_id="vm001", kernel_path=kernel, rootfs_path=rootfs))
    state.update_vm("vm001", status=status, pid=pid)
    return state


@pytest.mark.parametrize("pid", [0, -1, -2, -12345])
def test_memory_reconcile_marks_non_positive_pid_stale_without_signalling(
    tmp_path: Path,
    pid: int,
) -> None:
    """PID 0/negative are not VM processes and must not reach ``os.kill``."""
    state = _manager_with_vm(tmp_path, pid=pid)

    with mock.patch(
        "smolvm.storage._memory.os.kill",
        side_effect=AssertionError("reconcile must not probe a non-positive PID"),
    ):
        assert "vm001" in state.reconcile()

    vm_info = state.get_vm("vm001")
    assert vm_info.status == VMState.ERROR
    assert vm_info.pid is None


def test_memory_reconcile_marks_missing_pid_stale_without_signalling(tmp_path: Path) -> None:
    """A RUNNING VM with no recorded PID is stale and is never probed."""
    state = _manager_with_vm(tmp_path, pid=None)

    with mock.patch(
        "smolvm.storage._memory.os.kill",
        side_effect=AssertionError("reconcile must not probe a missing PID"),
    ):
        assert state.reconcile() == ["vm001"]

    vm_info = state.get_vm("vm001")
    assert vm_info.status == VMState.ERROR
    assert vm_info.pid is None


@pytest.mark.parametrize("status", [VMState.RUNNING, VMState.PAUSED])
def test_memory_reconcile_keeps_live_positive_pid(tmp_path: Path, status: VMState) -> None:
    """Control: a live process keeps its status and PID."""
    state = _manager_with_vm(tmp_path, status=status, pid=4242)

    with mock.patch("smolvm.storage._memory.os.kill", return_value=None) as kill:
        assert state.reconcile() == []

    kill.assert_called_once_with(4242, 0)
    vm_info = state.get_vm("vm001")
    assert vm_info.status == status
    assert vm_info.pid == 4242


def test_memory_reconcile_marks_dead_positive_pid_stale(tmp_path: Path) -> None:
    """A positive PID whose process is gone is reconciled to ERROR."""
    state = _manager_with_vm(tmp_path, pid=4242)

    with mock.patch("smolvm.storage._memory.os.kill", side_effect=ProcessLookupError):
        assert state.reconcile() == ["vm001"]

    vm_info = state.get_vm("vm001")
    assert vm_info.status == VMState.ERROR
    assert vm_info.pid is None


def test_memory_reconcile_treats_permission_error_as_alive(tmp_path: Path) -> None:
    """PermissionError means the process exists but belongs to another user."""
    state = _manager_with_vm(tmp_path, pid=4242)

    with mock.patch("smolvm.storage._memory.os.kill", side_effect=PermissionError):
        assert state.reconcile() == []

    vm_info = state.get_vm("vm001")
    assert vm_info.status == VMState.RUNNING
    assert vm_info.pid == 4242


def test_memory_reconcile_returns_each_stale_vm_once(tmp_path: Path) -> None:
    """A stale VM is reported once and is no longer stale on the next pass."""
    state = _manager_with_vm(tmp_path, pid=0)

    with mock.patch(
        "smolvm.storage._memory.os.kill",
        side_effect=AssertionError("reconcile must not probe a non-positive PID"),
    ):
        assert state.reconcile() == ["vm001"]
        # ERROR is neither RUNNING nor PAUSED, so the second pass skips it.
        assert state.reconcile() == []
