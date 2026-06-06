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

import json
import shutil
import subprocess
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
    SnapshotType,
    VMConfig,
    VMInfo,
    VMState,
    VsockConfig,
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


@pytest.fixture(autouse=True)
def _mock_qcow2_inspection_for_fake_snapshot_disks(
    request: pytest.FixtureRequest,
):
    """Keep mocked snapshot lifecycle tests independent of host qemu-img."""
    real_qemu_img_tests = {
        "test_full_snapshot_copy_preserves_internal_snapshot_on_backed_overlay",
        "test_full_snapshot_copy_requires_qemu_img",
        "test_restored_full_snapshot_disk_survives_snapshot_dir_delete",
        "test_restore_qemu_snapshot_removes_replaced_disk_sidecars",
        "test_async_delete_qemu_vm_removes_restored_backing_sidecars",
        "test_delete_qemu_vm_removes_restored_backing_sidecars",
    }
    if request.node.name in real_qemu_img_tests:
        yield
        return

    with patch.object(QemuRuntimeAdapter, "_qcow2_backing_file_required", return_value=None):
        yield


def _create_qemu_vm(sdk: SmolVMManager, config: VMConfig) -> None:
    with patch.object(SmolVMManager, "_create_qemu_overlay_disk") as mock_convert:
        mock_convert.side_effect = (
            lambda source, target, **_kwargs: target.write_text("managed-qcow2")
        )
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


def test_full_snapshot_copy_preserves_internal_snapshot_on_backed_overlay(
    tmp_path: Path,
) -> None:
    """Full QEMU snapshot copies must keep internal VM-state snapshot tags."""
    qemu_img = shutil.which("qemu-img")
    if qemu_img is None:
        pytest.skip("qemu-img is not installed")

    base = tmp_path / "base.qcow2"
    overlay = tmp_path / "overlay.qcow2"
    dest = tmp_path / "snapshot" / "disk.qcow2"
    dest.parent.mkdir()
    subprocess.run([qemu_img, "create", "-f", "qcow2", str(base), "10M"], check=True)
    subprocess.run(
        [qemu_img, "create", "-f", "qcow2", "-b", str(base), "-F", "qcow2", str(overlay)],
        check=True,
    )
    subprocess.run([qemu_img, "snapshot", "-c", "snap-001", str(overlay)], check=True)

    QemuRuntimeAdapter._copy_disk_standalone(overlay, dest)

    snapshots = subprocess.run(
        [qemu_img, "snapshot", "-l", str(dest)],
        capture_output=True,
        text=True,
        check=True,
    )
    info = subprocess.run(
        [qemu_img, "info", "--output=json", str(dest)],
        capture_output=True,
        text=True,
        check=True,
    )
    backing_path = Path(json.loads(info.stdout)["full-backing-filename"])
    assert "snap-001" in snapshots.stdout
    assert backing_path.parent == dest.parent
    assert backing_path.exists()


def test_full_snapshot_copy_requires_qemu_img(tmp_path: Path) -> None:
    """Full snapshots must fail rather than shallow-copy when disk layout is unknown."""
    source = tmp_path / "disk.qcow2"
    dest = tmp_path / "snapshot.qcow2"
    source.touch()

    with (
        patch("smolvm.runtime.qemu.which", return_value=None),
        pytest.raises(SmolVMError, match="qemu-img") as exc_info,
    ):
        QemuRuntimeAdapter._copy_disk_standalone(source, dest)

    message = str(exc_info.value)
    assert "sudo apt-get install -y qemu-utils" in message
    assert "sudo dnf install -y qemu-img" in message
    assert "sudo yum install -y qemu-img" in message
    assert "sudo pacman -S --needed qemu-base" in message
    assert not dest.exists()


def test_restored_full_snapshot_disk_survives_snapshot_dir_delete(tmp_path: Path) -> None:
    """Restored full snapshot disks should not depend on the snapshot directory."""
    qemu_img = shutil.which("qemu-img")
    if qemu_img is None:
        pytest.skip("qemu-img is not installed")

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snap"
    disk_dir = tmp_path / "data" / "disks"
    source_dir.mkdir()
    snapshot_dir.mkdir()
    disk_dir.mkdir(parents=True)
    base = source_dir / "base.qcow2"
    overlay = source_dir / "overlay.qcow2"
    snapshot_disk = snapshot_dir / "disk.qcow2"
    restore_disk = disk_dir / "vm001.qcow2.restore-test"
    managed_disk = disk_dir / "vm001.qcow2"
    subprocess.run([qemu_img, "create", "-f", "qcow2", str(base), "10M"], check=True)
    subprocess.run(
        [qemu_img, "create", "-f", "qcow2", "-b", str(base), "-F", "qcow2", str(overlay)],
        check=True,
    )
    subprocess.run([qemu_img, "snapshot", "-c", "snap-001", str(overlay)], check=True)
    QemuRuntimeAdapter._copy_disk_standalone(overlay, snapshot_disk)

    QemuRuntimeAdapter._copy_disk_standalone(snapshot_disk, restore_disk)
    restore_disk.replace(managed_disk)
    shutil.rmtree(snapshot_dir)

    info = subprocess.run(
        [qemu_img, "info", "--output=json", str(managed_disk)],
        capture_output=True,
        text=True,
        check=True,
    )
    backing_path = Path(json.loads(info.stdout)["full-backing-filename"])
    snapshots = subprocess.run(
        [qemu_img, "snapshot", "-l", str(managed_disk)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert backing_path.parent == disk_dir
    assert backing_path.exists()
    assert "snap-001" in snapshots.stdout


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
        vm_config=vm_info.config.model_copy(update={"rootfs_format": None}),
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
    assert restored.config.rootfs_format == "qcow2"
    assert restored_snapshot.restored is True
    assert managed_disk.read_text() == "snapshotted-qcow2"
    mock_client.snapshot_load.assert_called_once()
    mock_client.snapshot_delete.assert_called_once()


def test_restore_validates_artifacts_before_stopping_existing_vm(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
) -> None:
    """A bad snapshot should not stop the active VM before failing."""
    _create_qemu_vm(qemu_smol_vm, qemu_config)
    vm_info = qemu_smol_vm.get("vm001")
    qemu_smol_vm.state.update_vm("vm001", status=VMState.RUNNING, pid=12345)
    snapshot = SnapshotInfo(
        snapshot_id="snap-missing-disk",
        vm_id="vm001",
        backend="qemu",
        artifacts=SnapshotArtifacts(disk_path=qemu_smol_vm.snapshot_dir / "missing.qcow2"),
        vm_config=vm_info.config,
        network_config=vm_info.network,
        created_at=datetime.now(timezone.utc),
    )
    qemu_smol_vm.state.create_snapshot(snapshot)

    with (
        patch.object(qemu_smol_vm, "stop") as mock_stop,
        pytest.raises(SmolVMError, match="disk_path"),
    ):
        qemu_smol_vm.restore_snapshot("snap-missing-disk")

    mock_stop.assert_not_called()


def test_restore_qemu_snapshot_reserves_persisted_vsock_cid(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
) -> None:
    """Restored QEMU VMs must keep their vsock CID tracked in state."""
    qemu_config = qemu_config.model_copy(
        update={"vsock": VsockConfig(guest_cid=42), "comm_channel": "ssh"}
    )
    _create_qemu_vm(qemu_smol_vm, qemu_config)
    vm_info = qemu_smol_vm.get("vm001")

    snapshot_dir = qemu_smol_vm.snapshot_dir / "snap-vsock"
    snapshot_dir.mkdir(parents=True)
    snapshot = SnapshotInfo(
        snapshot_id="snap-vsock",
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
        qemu_smol_vm.restore_snapshot("snap-vsock")

    assert qemu_smol_vm.state.get_vsock_cid("vm001") == 42


def test_restore_qemu_snapshot_persistence_failure_removes_placeholder(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
) -> None:
    """If restore cannot create the VM row, the managed placeholder is removed."""
    snapshot_dir = qemu_smol_vm.snapshot_dir / "snap-persist-fail"
    snapshot_dir.mkdir(parents=True)
    snapshot = SnapshotInfo(
        snapshot_id="snap-persist-fail",
        vm_id="vm001",
        backend="qemu",
        artifacts=SnapshotArtifacts(disk_path=snapshot_dir / "disk.qcow2"),
        vm_config=qemu_config,
        network_config=NetworkConfig(
            guest_ip="10.0.2.15",
            tap_device="qemu-user",
            guest_mac="52:54:00:5d:00:04",
            ssh_host_port=2205,
        ),
        created_at=datetime.now(timezone.utc),
    )
    snapshot.artifacts.disk_path.write_text("snapshotted-qcow2")
    qemu_smol_vm.state.create_snapshot(snapshot)

    with (
        patch.object(qemu_smol_vm.state, "create_vm", side_effect=SmolVMError("persist failed")),
        pytest.raises(SmolVMError, match="persist failed"),
    ):
        qemu_smol_vm.restore_snapshot("snap-persist-fail")

    assert not (qemu_smol_vm.data_dir / "disks" / "vm001.qcow2").exists()


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


def test_restore_qemu_snapshot_restores_backup_when_status_update_fails(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
) -> None:
    """Rollback should restore the original disk even if state status update fails."""
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

    with (
        patch.object(qemu_smol_vm.state, "update_vm", side_effect=SmolVMError("state failed")),
        pytest.raises(SmolVMError, match="state failed"),
    ):
        qemu_smol_vm.restore_snapshot("snap-001")

    assert managed_disk.read_text() == "original-managed-qcow2"


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


def test_restore_qemu_snapshot_removes_replaced_disk_sidecars(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Re-restoring over an existing full restore should not leak old sidecars."""
    qemu_img = shutil.which("qemu-img")
    if qemu_img is None:
        pytest.skip("qemu-img is not installed")

    managed_disk = qemu_smol_vm.data_dir / "disks" / "vm001.qcow2"
    old_sidecar = managed_disk.with_name(f"{managed_disk.name}.restore-old.backing-0.qcow2")
    subprocess.run([qemu_img, "create", "-f", "qcow2", str(old_sidecar), "10M"], check=True)
    subprocess.run(
        [
            qemu_img,
            "create",
            "-f",
            "qcow2",
            "-b",
            str(old_sidecar),
            "-F",
            "qcow2",
            str(managed_disk),
        ],
        check=True,
    )
    config = qemu_config.model_copy(update={"rootfs_path": managed_disk, "rootfs_format": "qcow2"})
    qemu_smol_vm.state.create_vm(config)
    qemu_smol_vm.state.update_vm(
        "vm001",
        network=NetworkConfig(
            guest_ip="10.0.2.15",
            tap_device="qemu-user",
            guest_mac="52:54:00:5d:00:03",
            ssh_host_port=2204,
        ),
    )

    snapshot_dir = qemu_smol_vm.snapshot_dir / "snap-replace"
    snapshot_dir.mkdir(parents=True)
    snapshot_disk = snapshot_dir / "disk.qcow2"
    subprocess.run([qemu_img, "create", "-f", "qcow2", str(snapshot_disk), "10M"], check=True)
    snapshot = SnapshotInfo(
        snapshot_id="snap-replace",
        vm_id="vm001",
        backend="qemu",
        artifacts=SnapshotArtifacts(disk_path=snapshot_disk),
        vm_config=config,
        network_config=qemu_smol_vm.state.get_vm("vm001").network,
        created_at=datetime.now(timezone.utc),
    )
    qemu_smol_vm.state.create_snapshot(snapshot)
    process = MagicMock()
    process.pid = 98765
    process.poll.return_value = None

    with (
        patch.object(qemu_smol_vm, "_start_qemu", return_value=process),
        patch("smolvm.runtime.qemu.QMPClient") as mock_client_cls,
    ):
        mock_client = _mock_qmp_client()
        mock_client_cls.return_value = mock_client
        qemu_smol_vm.restore_snapshot("snap-replace")

    assert not old_sidecar.exists()


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


def test_snapshot_rejected_for_windows_guests(qemu_smol_vm: SmolVMManager, tmp_path: Path) -> None:
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


@pytest.mark.asyncio
async def test_async_delete_qemu_vm_removes_restored_backing_sidecars(
    qemu_smol_vm: SmolVMManager,
    tmp_path: Path,
) -> None:
    """async_delete should mirror sync cleanup for restored backing sidecars."""
    qemu_img = shutil.which("qemu-img")
    if qemu_img is None:
        pytest.skip("qemu-img is not installed")

    kernel = tmp_path / "vmlinux"
    kernel.touch()
    managed_disk = qemu_smol_vm.data_dir / "disks" / "vm-async-sidecar.qcow2"
    sidecar = managed_disk.with_name(f"{managed_disk.name}.restore-test.backing-0.qcow2")
    subprocess.run([qemu_img, "create", "-f", "qcow2", str(sidecar), "10M"], check=True)
    subprocess.run(
        [
            qemu_img,
            "create",
            "-f",
            "qcow2",
            "-b",
            str(sidecar),
            "-F",
            "qcow2",
            str(managed_disk),
        ],
        check=True,
    )
    config = VMConfig(
        vm_id="vm-async-sidecar",
        kernel_path=kernel,
        rootfs_path=managed_disk,
        rootfs_format="qcow2",
        backend="qemu",
    )
    qemu_smol_vm.state.create_vm(config)

    await qemu_smol_vm.async_delete("vm-async-sidecar")

    assert not managed_disk.exists()
    assert not sidecar.exists()


def test_delete_qemu_vm_removes_restored_backing_sidecars(
    qemu_smol_vm: SmolVMManager,
    tmp_path: Path,
) -> None:
    """Deleting a restored full snapshot should remove local backing sidecars too."""
    qemu_img = shutil.which("qemu-img")
    if qemu_img is None:
        pytest.skip("qemu-img is not installed")

    kernel = tmp_path / "vmlinux"
    kernel.touch()
    managed_disk = qemu_smol_vm.data_dir / "disks" / "vm-sidecar.qcow2"
    sidecar = managed_disk.with_name(f"{managed_disk.name}.restore-test.backing-0.qcow2")
    subprocess.run([qemu_img, "create", "-f", "qcow2", str(sidecar), "10M"], check=True)
    subprocess.run(
        [
            qemu_img,
            "create",
            "-f",
            "qcow2",
            "-b",
            str(sidecar),
            "-F",
            "qcow2",
            str(managed_disk),
        ],
        check=True,
    )
    config = VMConfig(
        vm_id="vm-sidecar",
        kernel_path=kernel,
        rootfs_path=managed_disk,
        rootfs_format="qcow2",
        backend="qemu",
    )
    qemu_smol_vm.state.create_vm(config)

    qemu_smol_vm.delete("vm-sidecar")

    assert not managed_disk.exists()
    assert not sidecar.exists()


def test_snapshot_rejected_for_raw_qemu_disks(
    qemu_smol_vm: SmolVMManager,
    tmp_path: Path,
) -> None:
    """QEMU snapshot code only supports qcow2 managed disks today."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "vm-raw.ext4"
    kernel.touch()
    rootfs.touch()
    config = VMConfig(
        vm_id="vm-raw-snap",
        kernel_path=kernel,
        rootfs_path=rootfs,
        rootfs_format="raw-ext4",
        backend="qemu",
    )
    vm_info = VMInfo(
        vm_id="vm-raw-snap",
        status=VMState.RUNNING,
        config=config,
        network=NetworkConfig(
            guest_ip="10.0.2.15",
            tap_device="qemu-user",
            guest_mac="52:54:00:5d:00:02",
            ssh_host_port=2203,
        ),
    )

    with pytest.raises(SmolVMError, match="raw QEMU disks"):
        qemu_smol_vm._ensure_snapshot_supported(vm_info)


def test_create_qemu_snapshot_defaults_to_full_type(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
    tmp_path: Path,
) -> None:
    """QEMU snapshots default to full for self-contained restore."""
    _running_qemu_vm(qemu_smol_vm, qemu_config, tmp_path)

    with patch("smolvm.runtime.qemu.QMPClient") as mock_client_cls:
        mock_client = _mock_qmp_client()
        mock_client_cls.return_value = mock_client
        snapshot = qemu_smol_vm.create_snapshot("vm001", snapshot_id="snap-full")

    assert snapshot.snapshot_type is SnapshotType.FULL
    assert qemu_smol_vm.state.get_snapshot("snap-full").snapshot_type is SnapshotType.FULL


def test_create_qemu_disk_snapshot_uses_internal_sync_not_vmstate(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
    tmp_path: Path,
) -> None:
    """A disk-only snapshot must skip the RAM-dumping snapshot-save job.

    It uses the synchronous block-device internal snapshot instead, and still
    produces a self-contained disk artifact.
    """
    _running_qemu_vm(qemu_smol_vm, qemu_config, tmp_path)

    with patch("smolvm.runtime.qemu.QMPClient") as mock_client_cls:
        mock_client = _mock_qmp_client()
        mock_client_cls.return_value = mock_client
        snapshot = qemu_smol_vm.create_snapshot(
            "vm001", snapshot_id="snap-disk", snapshot_type=SnapshotType.DISK
        )

    # No guest RAM dumped: the heavyweight job API is never touched.
    mock_client.snapshot_save.assert_not_called()
    mock_client.snapshot_delete.assert_not_called()
    # Disk-only internal snapshot taken and cleaned up synchronously.
    mock_client.blockdev_snapshot_internal_sync.assert_called_once()
    mock_client.blockdev_snapshot_delete_internal_sync.assert_called_once()

    persisted = qemu_smol_vm.state.get_snapshot("snap-disk")
    assert snapshot.snapshot_type is SnapshotType.DISK
    assert persisted.snapshot_type is SnapshotType.DISK
    # Self-contained standalone copy (like FULL), not an overlay.
    assert persisted.artifacts.disk_path.read_text() == "managed-qcow2"
    assert qemu_smol_vm.get("vm001").status == VMState.PAUSED


def test_restore_qemu_disk_snapshot_boots_fresh_without_loading_vmstate(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
) -> None:
    """Restoring a disk-only snapshot boots fresh: no snapshot-load of RAM."""
    _create_qemu_vm(qemu_smol_vm, qemu_config)
    vm_info = qemu_smol_vm.get("vm001")

    snapshot_dir = qemu_smol_vm.snapshot_dir / "snap-disk"
    snapshot_dir.mkdir(parents=True)
    snapshot = SnapshotInfo(
        snapshot_id="snap-disk",
        vm_id="vm001",
        backend="qemu",
        artifacts=SnapshotArtifacts(disk_path=snapshot_dir / "disk.qcow2"),
        vm_config=vm_info.config,
        network_config=vm_info.network,
        created_at=datetime.now(timezone.utc),
        snapshot_type=SnapshotType.DISK,
    )
    snapshot.artifacts.disk_path.write_text("disk-only-qcow2")
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
        restored = qemu_smol_vm.restore_snapshot("snap-disk", resume_vm=True)

    managed_disk = qemu_smol_vm.data_dir / "disks" / "vm001.qcow2"
    assert managed_disk.read_text() == "disk-only-qcow2"
    # Fresh boot: never resumes saved RAM, just continues the cold boot.
    mock_client.snapshot_load.assert_not_called()
    mock_client.cont.assert_called_once()
    assert restored.status == VMState.RUNNING


def test_create_qemu_diff_snapshot_records_type(
    qemu_smol_vm: SmolVMManager,
    qemu_config: VMConfig,
    tmp_path: Path,
) -> None:
    """A diff QEMU snapshot records its type and takes the overlay copy path."""
    _running_qemu_vm(qemu_smol_vm, qemu_config, tmp_path)

    with (
        patch("smolvm.runtime.qemu.QMPClient") as mock_client_cls,
        patch.object(
            QemuRuntimeAdapter,
            "_copy_disk_overlay",
            side_effect=lambda source, dest: dest.write_text(Path(source).read_text()),
        ) as mock_overlay,
    ):
        mock_client = _mock_qmp_client()
        mock_client_cls.return_value = mock_client
        snapshot = qemu_smol_vm.create_snapshot(
            "vm001", snapshot_id="snap-diff", snapshot_type=SnapshotType.DIFF
        )

    mock_overlay.assert_called_once()
    persisted = qemu_smol_vm.state.get_snapshot("snap-diff")
    assert snapshot.snapshot_type is SnapshotType.DIFF
    assert persisted.snapshot_type is SnapshotType.DIFF
    assert persisted.artifacts.disk_path.read_text() == "managed-qcow2"
