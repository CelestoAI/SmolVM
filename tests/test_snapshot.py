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

"""Tests for VM snapshot lifecycle management."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import SmolVMError, SnapshotNotFoundError, VMNotFoundError
from smolvm.types import SnapshotInfo, VMConfig, VMState
from smolvm.vm import SmolVMManager


@pytest.fixture
def smol_vm(tmp_path: Path) -> SmolVMManager:
    """Create a SmolVM instance with temporary directories."""
    sdk = SmolVMManager(
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sockets",
        backend="firecracker",
    )
    sdk.network = MagicMock()
    sdk.network.host_ip = "172.16.0.1"
    sdk.network.generate_mac.return_value = "AA:FC:00:00:00:01"
    return sdk


@pytest.fixture
def sample_config(tmp_path: Path) -> VMConfig:
    """Create a sample VMConfig."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.write_text("rootfs-data")

    return VMConfig(
        vm_id="vm001",
        vcpu_count=2,
        mem_size_mib=512,
        kernel_path=kernel,
        rootfs_path=rootfs,
    )


def _running_vm(smol_vm: SmolVMManager, config: VMConfig, tmp_path: Path) -> Path:
    vm_info = smol_vm.create(config)
    socket_path = tmp_path / "sockets" / "fc-vm001.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.touch()
    smol_vm.state.update_vm(
        vm_info.vm_id,
        status=VMState.RUNNING,
        pid=12345,
        socket_path=socket_path,
    )
    return socket_path


def test_pause_and_resume_firecracker_vm(
    smol_vm: SmolVMManager,
    sample_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Pause/resume should transition the persisted VM state."""
    _running_vm(smol_vm, sample_config, tmp_path)

    with patch("smolvm.vm.FirecrackerClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        paused = smol_vm.pause("vm001")
        resumed = smol_vm.resume("vm001")

    assert paused.status == VMState.PAUSED
    assert resumed.status == VMState.RUNNING
    mock_client.pause_vm.assert_called_once()
    mock_client.resume_vm.assert_called_once()


def test_pause_and_resume_support_shared_disk_firecracker_vm(
    smol_vm: SmolVMManager,
    sample_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Pause/resume should not enforce snapshot-only disk restrictions."""
    shared_config = sample_config.model_copy(update={"disk_mode": "shared"})
    _running_vm(smol_vm, shared_config, tmp_path)

    with patch("smolvm.vm.FirecrackerClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        paused = smol_vm.pause("vm001")
        resumed = smol_vm.resume("vm001")

    assert paused.status == VMState.PAUSED
    assert resumed.status == VMState.RUNNING
    mock_client.pause_vm.assert_called_once()
    mock_client.resume_vm.assert_called_once()


def test_create_snapshot_pauses_vm_and_persists_metadata(
    smol_vm: SmolVMManager,
    sample_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Snapshot creation should write snapshot metadata and leave the source paused."""
    _running_vm(smol_vm, sample_config, tmp_path)
    managed_disk = smol_vm.data_dir / "disks" / "vm001.ext4"
    managed_disk.write_text("managed-disk")

    def _write_snapshot(snapshot_path: Path, mem_path: Path, snapshot_type: str = "Full") -> None:
        snapshot_path.write_text("vmstate")
        mem_path.write_text("memory")

    with patch("smolvm.vm.FirecrackerClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.create_snapshot.side_effect = _write_snapshot
        mock_client_cls.return_value = mock_client

        snapshot = smol_vm.create_snapshot("vm001", snapshot_id="snap-001")

    persisted = smol_vm.state.get_snapshot("snap-001")

    assert snapshot.snapshot_id == "snap-001"
    assert snapshot.disk_path.read_text() == "managed-disk"
    assert persisted.vm_config.rootfs_path == managed_disk
    assert smol_vm.get("vm001").status == VMState.PAUSED
    mock_client.pause_vm.assert_called_once()
    mock_client.create_snapshot.assert_called_once()


def test_create_snapshot_rolls_back_metadata_on_resume_failure(
    smol_vm: SmolVMManager,
    sample_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Snapshot rollback should remove persisted metadata after a late failure."""
    _running_vm(smol_vm, sample_config, tmp_path)
    managed_disk = smol_vm.data_dir / "disks" / "vm001.ext4"
    managed_disk.write_text("managed-disk")

    def _write_snapshot(snapshot_path: Path, mem_path: Path, snapshot_type: str = "Full") -> None:
        snapshot_path.write_text("vmstate")
        mem_path.write_text("memory")

    with patch("smolvm.vm.FirecrackerClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.create_snapshot.side_effect = _write_snapshot
        mock_client.resume_vm.side_effect = [SmolVMError("resume failed"), None]
        mock_client_cls.return_value = mock_client

        with pytest.raises(SmolVMError, match="resume failed"):
            smol_vm.create_snapshot("vm001", snapshot_id="snap-001", resume_source=True)

    with pytest.raises(SnapshotNotFoundError):
        smol_vm.state.get_snapshot("snap-001")
    assert not (smol_vm.snapshot_dir / "snap-001").exists()
    assert smol_vm.get("vm001").status == VMState.RUNNING


def test_create_snapshot_does_not_create_dir_when_client_lookup_fails(
    smol_vm: SmolVMManager,
    sample_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Snapshot creation should not leave an empty directory on client setup failure."""
    socket_path = _running_vm(smol_vm, sample_config, tmp_path)
    socket_path.unlink()

    with pytest.raises(SmolVMError, match="socket"):
        smol_vm.create_snapshot("vm001", snapshot_id="snap-001")

    assert not (smol_vm.snapshot_dir / "snap-001").exists()


def test_create_snapshot_preserves_metadata_when_rollback_dir_cleanup_fails(
    smol_vm: SmolVMManager,
    sample_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Rollback should not delete metadata if the snapshot directory cannot be removed."""
    _running_vm(smol_vm, sample_config, tmp_path)
    managed_disk = smol_vm.data_dir / "disks" / "vm001.ext4"
    managed_disk.write_text("managed-disk")

    def _write_snapshot(snapshot_path: Path, mem_path: Path, snapshot_type: str = "Full") -> None:
        snapshot_path.write_text("vmstate")
        mem_path.write_text("memory")

    with patch("smolvm.vm.FirecrackerClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.create_snapshot.side_effect = _write_snapshot
        mock_client.resume_vm.side_effect = [SmolVMError("resume failed"), None]
        mock_client_cls.return_value = mock_client

        with (
            patch("smolvm.vm.shutil.rmtree", side_effect=PermissionError("cleanup denied")),
            pytest.raises(PermissionError, match="cleanup denied"),
        ):
            smol_vm.create_snapshot("vm001", snapshot_id="snap-001", resume_source=True)

    assert smol_vm.state.get_snapshot("snap-001").snapshot_id == "snap-001"
    assert (smol_vm.snapshot_dir / "snap-001").exists()
    assert smol_vm.get("vm001").status == VMState.RUNNING


@pytest.mark.parametrize("snapshot_id", ["/tmp/escape", "../escape", r"..\escape", "snap/001"])
def test_create_snapshot_rejects_unsafe_snapshot_id(
    smol_vm: SmolVMManager,
    sample_config: VMConfig,
    tmp_path: Path,
    snapshot_id: str,
) -> None:
    """Snapshot creation should reject IDs that could escape the snapshot directory."""
    _running_vm(smol_vm, sample_config, tmp_path)

    with pytest.raises(ValueError, match="snapshot_id"):
        smol_vm.create_snapshot("vm001", snapshot_id=snapshot_id)

    assert not any(smol_vm.snapshot_dir.iterdir())


def test_restore_snapshot_rehydrates_deleted_vm(
    smol_vm: SmolVMManager,
    sample_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Restoring should recreate the original VM record and managed disk."""
    smol_vm.create(sample_config)
    managed_disk = smol_vm.data_dir / "disks" / "vm001.ext4"
    managed_disk.write_text("managed-disk")
    vm_info = smol_vm.get("vm001")

    snapshot_dir = smol_vm.snapshot_dir / "snap-001"
    snapshot_dir.mkdir(parents=True)
    snapshot = SnapshotInfo(
        snapshot_id="snap-001",
        vm_id="vm001",
        snapshot_path=snapshot_dir / "vmstate.bin",
        mem_file_path=snapshot_dir / "mem.bin",
        disk_path=snapshot_dir / "disk.ext4",
        vm_config=vm_info.config,
        network_config=vm_info.network,
        created_at=datetime.now(timezone.utc),
    )
    snapshot.snapshot_path.write_text("vmstate")
    snapshot.mem_file_path.write_text("memory")
    snapshot.disk_path.write_text("snapshotted-disk")
    smol_vm.state.create_snapshot(snapshot)

    smol_vm.delete("vm001")
    assert not managed_disk.exists()

    smol_vm.network.reset_mock()

    with (
        patch.object(smol_vm, "_start_firecracker", return_value=SimpleNamespace(pid=98765)),
        patch("smolvm.vm.FirecrackerClient") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        restored = smol_vm.restore_snapshot("snap-001")

    restored_vm = smol_vm.get("vm001")
    restored_snapshot = smol_vm.state.get_snapshot("snap-001")

    assert restored.status == VMState.PAUSED
    assert restored_vm.status == VMState.PAUSED
    assert managed_disk.read_text() == "snapshotted-disk"
    assert restored_snapshot.restored is True
    smol_vm.network.create_tap.assert_called_once()
    smol_vm.network.setup_nat.assert_called_once()
    mock_client.wait_for_socket.assert_called_once()
    mock_client.load_snapshot.assert_called_once()


def test_restore_snapshot_rolls_back_new_vm_resources_on_failure(
    smol_vm: SmolVMManager,
    sample_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Failed restores should unwind resources for a newly recreated VM."""
    smol_vm.create(sample_config)
    managed_disk = smol_vm.data_dir / "disks" / "vm001.ext4"
    managed_disk.write_text("managed-disk")
    vm_info = smol_vm.get("vm001")

    snapshot_dir = smol_vm.snapshot_dir / "snap-001"
    snapshot_dir.mkdir(parents=True)
    snapshot = SnapshotInfo(
        snapshot_id="snap-001",
        vm_id="vm001",
        snapshot_path=snapshot_dir / "vmstate.bin",
        mem_file_path=snapshot_dir / "mem.bin",
        disk_path=snapshot_dir / "disk.ext4",
        vm_config=vm_info.config,
        network_config=vm_info.network,
        created_at=datetime.now(timezone.utc),
    )
    snapshot.snapshot_path.write_text("vmstate")
    snapshot.mem_file_path.write_text("memory")
    snapshot.disk_path.write_text("snapshotted-disk")
    smol_vm.state.create_snapshot(snapshot)

    smol_vm.delete("vm001")
    smol_vm.network.reset_mock()

    with (
        patch.object(smol_vm, "_start_firecracker", return_value=SimpleNamespace(pid=98765)),
        patch("smolvm.vm.FirecrackerClient") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client.load_snapshot.side_effect = SmolVMError("load failed")
        mock_client_cls.return_value = mock_client

        with pytest.raises(SmolVMError, match="load failed"):
            smol_vm.restore_snapshot("snap-001")

    with pytest.raises(VMNotFoundError):
        smol_vm.state.get_vm("vm001")
    assert smol_vm.state.get_ip_lease("vm001") is None
    assert smol_vm.state.get_ssh_port("vm001") is None
    assert not managed_disk.exists()
    smol_vm.network.cleanup_ssh_port_forward.assert_called_once()
    smol_vm.network.cleanup_nat_rules.assert_called_once()
    smol_vm.network.cleanup_tap.assert_called_once()


def test_delete_snapshot_rejects_active_restored_vm(
    smol_vm: SmolVMManager,
    sample_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Snapshots must not be deleted while their memory file backs a live VM."""
    _running_vm(smol_vm, sample_config, tmp_path)
    managed_disk = smol_vm.data_dir / "disks" / "vm001.ext4"
    managed_disk.write_text("managed-disk")

    def _write_snapshot(snapshot_path: Path, mem_path: Path, snapshot_type: str = "Full") -> None:
        snapshot_path.write_text("vmstate")
        mem_path.write_text("memory")

    with patch("smolvm.vm.FirecrackerClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.create_snapshot.side_effect = _write_snapshot
        mock_client_cls.return_value = mock_client
        smol_vm.create_snapshot("vm001", snapshot_id="snap-001")

    with (
        patch.object(smol_vm, "_start_firecracker", return_value=SimpleNamespace(pid=98765)),
        patch("smolvm.vm.FirecrackerClient") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        smol_vm.restore_snapshot("snap-001")

    with pytest.raises(SmolVMError, match="active"):
        smol_vm.delete_snapshot("snap-001")


def test_delete_snapshot_preserves_metadata_when_disk_cleanup_fails(
    smol_vm: SmolVMManager,
    sample_config: VMConfig,
    tmp_path: Path,
) -> None:
    """Snapshot metadata should remain when filesystem deletion fails."""
    smol_vm.create(sample_config)
    vm_info = smol_vm.get("vm001")

    snapshot_dir = smol_vm.snapshot_dir / "snap-001"
    snapshot_dir.mkdir(parents=True)
    snapshot = SnapshotInfo(
        snapshot_id="snap-001",
        vm_id="vm001",
        snapshot_path=snapshot_dir / "vmstate.bin",
        mem_file_path=snapshot_dir / "mem.bin",
        disk_path=snapshot_dir / "disk.ext4",
        vm_config=vm_info.config,
        network_config=vm_info.network,
        created_at=datetime.now(timezone.utc),
    )
    smol_vm.state.create_snapshot(snapshot)

    with (
        patch("smolvm.vm.shutil.rmtree", side_effect=PermissionError("denied")),
        pytest.raises(PermissionError, match="denied"),
    ):
        smol_vm.delete_snapshot("snap-001")

    assert smol_vm.state.get_snapshot("snap-001").snapshot_id == "snap-001"


@pytest.mark.parametrize("snapshot_id", ["/tmp/escape", "../escape", r"..\escape", "snap/001"])
def test_delete_snapshot_rejects_unsafe_snapshot_id(
    smol_vm: SmolVMManager,
    snapshot_id: str,
) -> None:
    """Snapshot deletion should reject IDs that could escape the snapshot directory."""
    with pytest.raises(ValueError, match="snapshot_id"):
        smol_vm.delete_snapshot(snapshot_id)
