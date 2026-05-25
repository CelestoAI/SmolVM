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

"""QEMU snapshot lifecycle regression tests."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import SmolVMError, VMNotFoundError
from smolvm.runtime.qemu import QemuRuntimeAdapter
from smolvm.types import (
    GuestOS,
    NetworkConfig,
    SnapshotArtifacts,
    SnapshotInfo,
    VMConfig,
    VMInfo,
    VMState,
)
from smolvm.vm import SmolVMManager


@pytest.fixture
def qemu_smol_vm(tmp_path: Path) -> SmolVMManager:
    """Create a QEMU-backed manager with mocked networking."""
    sdk = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="qemu",
    )
    sdk.network = MagicMock()
    sdk.network.generate_mac.return_value = "AA:FC:00:00:00:01"
    return sdk


@pytest.fixture
def qemu_config(tmp_path: Path) -> VMConfig:
    """Create a sample QEMU VM config."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.write_text("rootfs-data")
    return VMConfig(
        vm_id="vm001",
        vcpu_count=2,
        memory=512,
        kernel_path=kernel,
        rootfs_path=rootfs,
        backend="qemu",
        boot_args="console=ttyAMA0 reboot=k panic=1 init=/init",
    )


def _create_qemu_vm(sdk: SmolVMManager, config: VMConfig) -> None:
    with patch.object(SmolVMManager, "_create_qemu_overlay_disk") as mock_convert:
        mock_convert.side_effect = lambda source, target: target.write_text("managed-qcow2")
        sdk.create(config)


def _running_qemu_vm(sdk: SmolVMManager, config: VMConfig, tmp_path: Path) -> Path:
    _create_qemu_vm(sdk, config)
    control_socket_path = tmp_path / "sockets" / "qmp-vm001.sock"
    control_socket_path.parent.mkdir(parents=True, exist_ok=True)
    control_socket_path.touch()
    sdk.state.update_vm(
        "vm001",
        status=VMState.RUNNING,
        pid=12345,
        control_socket_path=control_socket_path,
    )
    return control_socket_path


def _mock_qmp_client() -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    return client


def test_wait_for_runtime_retries_when_qmp_greeting_times_out(tmp_path: Path) -> None:
    """QEMU may create the QMP socket before its greeting is readable."""
    context = MagicMock()
    adapter = QemuRuntimeAdapter(context)
    process = MagicMock()
    process.poll.return_value = None
    control_socket_path = tmp_path / "qmp-vm001.sock"
    attempts = 0

    class _ReadyClient:
        def __enter__(self) -> "_ReadyClient":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    def _client_side_effect(_path: Path, timeout: float = 5.0) -> _ReadyClient:
        nonlocal attempts
        del timeout
        attempts += 1
        if attempts == 1:
            raise TimeoutError("timed out")
        return _ReadyClient()

    with patch.object(adapter, "_client", side_effect=_client_side_effect):
        adapter._wait_for_runtime(process, control_socket_path, timeout=1.0)

    assert attempts == 2


def test_pause_and_resume_qemu_vm(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Pause/resume should drive QMP stop/cont for QEMU VMs."""
    _running_qemu_vm(qemu_smol_vm, qemu_config, tmp_path)

    with patch("smolvm.runtime.qemu.QMPClient") as mock_client_cls:
        mock_client = _mock_qmp_client()
        mock_client_cls.return_value = mock_client

        paused = qemu_smol_vm.pause("vm001")
        resumed = qemu_smol_vm.resume("vm001")

    assert paused.status == VMState.PAUSED
    assert resumed.status == VMState.RUNNING
    mock_client.stop_vm.assert_called_once()
    mock_client.cont.assert_called_once()


def test_create_qemu_snapshot_from_running_vm_leaves_source_paused_by_default(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
    tmp_path: Path,
) -> None:
    """QEMU snapshot creation should persist backend-neutral metadata and pause the source."""
    _running_qemu_vm(qemu_smol_vm, qemu_config, tmp_path)

    with patch("smolvm.runtime.qemu.QMPClient") as mock_client_cls:
        mock_client = _mock_qmp_client()
        mock_client_cls.return_value = mock_client

        snapshot = qemu_smol_vm.create_snapshot("vm001", snapshot_id="snap-001")

    persisted = qemu_smol_vm.state.get_snapshot("snap-001")
    assert snapshot.backend == "qemu"
    assert snapshot.artifacts.state_path is None
    assert snapshot.artifacts.memory_path is None
    assert snapshot.artifacts.disk_path.read_text() == "managed-qcow2"
    assert persisted.artifacts.disk_path.name == "disk.qcow2"
    assert qemu_smol_vm.get("vm001").status == VMState.PAUSED
    mock_client.stop_vm.assert_called_once()
    mock_client.cont.assert_not_called()
    mock_client.snapshot_save.assert_called_once()
    mock_client.snapshot_delete.assert_called_once()


def test_create_qemu_snapshot_from_running_vm_can_resume_source(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
    tmp_path: Path,
) -> None:
    """QEMU snapshot creation should optionally resume the source VM after persistence."""
    _running_qemu_vm(qemu_smol_vm, qemu_config, tmp_path)

    with patch("smolvm.runtime.qemu.QMPClient") as mock_client_cls:
        mock_client = _mock_qmp_client()
        mock_client_cls.return_value = mock_client

        qemu_smol_vm.create_snapshot("vm001", snapshot_id="snap-001", resume_source=True)

    assert qemu_smol_vm.get("vm001").status == VMState.RUNNING
    mock_client.cont.assert_called_once()


def test_create_qemu_snapshot_from_paused_vm_does_not_stop_again(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
) -> None:
    """Snapshotting an already paused QEMU VM should skip an extra stop command."""
    _create_qemu_vm(qemu_smol_vm, qemu_config)
    control_socket_path = qemu_smol_vm.socket_dir / "qmp-vm001.sock"
    control_socket_path.parent.mkdir(parents=True, exist_ok=True)
    control_socket_path.touch()
    qemu_smol_vm.state.update_vm(
        "vm001",
        status=VMState.PAUSED,
        pid=12345,
        control_socket_path=control_socket_path,
    )

    with patch("smolvm.runtime.qemu.QMPClient") as mock_client_cls:
        mock_client = _mock_qmp_client()
        mock_client_cls.return_value = mock_client

        qemu_smol_vm.create_snapshot("vm001", snapshot_id="snap-001")

    mock_client.stop_vm.assert_not_called()
    assert qemu_smol_vm.get("vm001").status == VMState.PAUSED


def test_restore_qemu_snapshot_rehydrates_deleted_vm(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
) -> None:
    """Restoring a QEMU snapshot should recreate the original VM identity and managed disk."""
    _create_qemu_vm(qemu_smol_vm, qemu_config)
    vm_info = qemu_smol_vm.get("vm001")

    snapshot_dir = qemu_smol_vm.snapshot_dir / "snap-001"
    snapshot_dir.mkdir(parents=True)
    snapshot = SnapshotInfo(
        snapshot_id="snap-001",
        vm_id="vm001",
        backend="qemu",
        artifacts=SnapshotArtifacts(disk_path=snapshot_dir / "disk.qcow2"),
        vm_config=vm_info.config,
        network_config=vm_info.network,
        created_at=datetime.now(timezone.utc),
    )
    snapshot.artifacts.disk_path.write_text("snapshotted-qcow2")
    qemu_smol_vm.state.create_snapshot(snapshot)

    qemu_smol_vm.delete("vm001")
    process = MagicMock()
    process.pid = 98765
    process.poll.return_value = None

    with (
        patch.object(qemu_smol_vm, "_start_qemu", return_value=process),
        patch("smolvm.runtime.qemu.QMPClient") as mock_client_cls,
    ):
        mock_client = _mock_qmp_client()
        mock_client_cls.return_value = mock_client
        restored = qemu_smol_vm.restore_snapshot("snap-001")

    restored_snapshot = qemu_smol_vm.state.get_snapshot("snap-001")
    managed_disk = qemu_smol_vm.data_dir / "disks" / "vm001.qcow2"
    assert restored.status == VMState.PAUSED
    assert restored_snapshot.restored is True
    assert managed_disk.read_text() == "snapshotted-qcow2"
    mock_client.snapshot_load.assert_called_once()
    mock_client.snapshot_delete.assert_called_once()


def test_restore_qemu_snapshot_rolls_back_new_vm_resources_on_failure(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
) -> None:
    """Failed QEMU restores should unwind the recreated VM record and managed disk."""
    _create_qemu_vm(qemu_smol_vm, qemu_config)
    vm_info = qemu_smol_vm.get("vm001")

    snapshot_dir = qemu_smol_vm.snapshot_dir / "snap-001"
    snapshot_dir.mkdir(parents=True)
    snapshot = SnapshotInfo(
        snapshot_id="snap-001",
        vm_id="vm001",
        backend="qemu",
        artifacts=SnapshotArtifacts(disk_path=snapshot_dir / "disk.qcow2"),
        vm_config=vm_info.config,
        network_config=vm_info.network,
        created_at=datetime.now(timezone.utc),
    )
    snapshot.artifacts.disk_path.write_text("snapshotted-qcow2")
    qemu_smol_vm.state.create_snapshot(snapshot)

    qemu_smol_vm.delete("vm001")
    process = MagicMock()
    process.pid = 98765
    process.poll.return_value = None

    with (
        patch.object(qemu_smol_vm, "_start_qemu", return_value=process),
        patch("smolvm.runtime.qemu.QMPClient") as mock_client_cls,
    ):
        mock_client = _mock_qmp_client()
        mock_client.wait_for_job.side_effect = SmolVMError("load failed")
        mock_client_cls.return_value = mock_client

        with pytest.raises(SmolVMError, match="load failed"):
            qemu_smol_vm.restore_snapshot("snap-001")

    with pytest.raises(VMNotFoundError):
        qemu_smol_vm.state.get_vm("vm001")
    assert qemu_smol_vm.state.get_ssh_port("vm001") is None
    assert not (qemu_smol_vm.data_dir / "disks" / "vm001.qcow2").exists()


def test_restore_qemu_snapshot_preserves_existing_managed_disk_on_failure(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
) -> None:
    """Failed restores should not clobber an existing QEMU managed disk."""
    _create_qemu_vm(qemu_smol_vm, qemu_config)
    managed_disk = qemu_smol_vm.data_dir / "disks" / "vm001.qcow2"
    managed_disk.write_text("original-managed-qcow2")
    vm_info = qemu_smol_vm.get("vm001")

    snapshot_dir = qemu_smol_vm.snapshot_dir / "snap-001"
    snapshot_dir.mkdir(parents=True)
    snapshot = SnapshotInfo(
        snapshot_id="snap-001",
        vm_id="vm001",
        backend="qemu",
        artifacts=SnapshotArtifacts(disk_path=snapshot_dir / "disk.qcow2"),
        vm_config=vm_info.config,
        network_config=vm_info.network,
        created_at=datetime.now(timezone.utc),
    )
    snapshot.artifacts.disk_path.write_text("snapshotted-qcow2")
    qemu_smol_vm.state.create_snapshot(snapshot)

    process = MagicMock()
    process.pid = 98765
    process.poll.return_value = None

    with (
        patch.object(qemu_smol_vm, "_start_qemu", return_value=process),
        patch("smolvm.runtime.qemu.QMPClient") as mock_client_cls,
    ):
        mock_client = _mock_qmp_client()
        mock_client.wait_for_job.side_effect = SmolVMError("load failed")
        mock_client_cls.return_value = mock_client

        with pytest.raises(SmolVMError, match="load failed"):
            qemu_smol_vm.restore_snapshot("snap-001")

    assert managed_disk.read_text() == "original-managed-qcow2"
    assert qemu_smol_vm.get("vm001").status == VMState.ERROR


def test_delete_qemu_snapshot_rejects_active_restored_vm(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
) -> None:
    """QEMU snapshots should not be deleted while their restored VM is active."""
    _create_qemu_vm(qemu_smol_vm, qemu_config)
    vm_info = qemu_smol_vm.get("vm001")

    snapshot_dir = qemu_smol_vm.snapshot_dir / "snap-001"
    snapshot_dir.mkdir(parents=True)
    snapshot = SnapshotInfo(
        snapshot_id="snap-001",
        vm_id="vm001",
        backend="qemu",
        artifacts=SnapshotArtifacts(disk_path=snapshot_dir / "disk.qcow2"),
        vm_config=vm_info.config,
        network_config=vm_info.network,
        created_at=datetime.now(timezone.utc),
    )
    snapshot.artifacts.disk_path.write_text("snapshotted-qcow2")
    qemu_smol_vm.state.create_snapshot(snapshot)

    qemu_smol_vm.delete("vm001")
    process = MagicMock()
    process.pid = 98765
    process.poll.return_value = None

    with (
        patch.object(qemu_smol_vm, "_start_qemu", return_value=process),
        patch("smolvm.runtime.qemu.QMPClient") as mock_client_cls,
    ):
        mock_client = _mock_qmp_client()
        mock_client_cls.return_value = mock_client
        qemu_smol_vm.restore_snapshot("snap-001", resume_vm=True)

    with pytest.raises(SmolVMError, match="active"):
        qemu_smol_vm.delete_snapshot("snap-001")


def test_snapshot_rejected_for_windows_guests(
    qemu_smol_vm: SmolVMManager, tmp_path: Path
) -> None:
    """Windows snapshot/restore is locked out in Phase 1 with a clear message."""
    rootfs = tmp_path / "win11.qcow2"
    rootfs.touch()
    config = VMConfig(
        vm_id="vm-win-snap",
        kernel_path=None,
        rootfs_path=rootfs,
        backend="qemu",
        guest_os=GuestOS.WINDOWS,
        boot_mode="firmware",
        disk_mode="shared",
    )
    vm_info = VMInfo(
        vm_id="vm-win-snap",
        status=VMState.RUNNING,
        config=config,
        network=NetworkConfig(
            guest_ip="10.0.2.15",
            tap_device="qemu-user",
            guest_mac="52:54:00:5d:00:01",
            ssh_host_port=2202,
        ),
    )
    with pytest.raises(SmolVMError, match="Windows guests"):
        qemu_smol_vm._ensure_snapshot_supported(vm_info)
