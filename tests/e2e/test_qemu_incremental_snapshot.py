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

"""Boot proof for QEMU persistent dirty-bitmap incremental artifacts."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path

import pytest

from smolvm import QemuDirtyBitmapBackup, SmolVM, SnapshotCapturePolicy, SnapshotType, VMConfig

pytestmark = pytest.mark.e2e


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


def _materialize(qemu_img: str, root: Path, name: str, artifacts: list[Path]) -> Path:
    staging = root / f"staging-{name}"
    staging.mkdir()
    local_chain: list[Path] = []
    for depth, artifact in enumerate(artifacts):
        local = staging / f"depth-{depth}.qcow2"
        shutil.copy2(artifact, local)
        local_chain.append(local)
        if depth > 0:
            _run(
                qemu_img,
                "rebase",
                "-u",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(local_chain[depth - 1]),
                str(local),
            )

    _run(qemu_img, "check", str(local_chain[-1]))
    output = root / f"leaf-{name}.qcow2"
    _run(
        qemu_img,
        "convert",
        "-f",
        "qcow2",
        "-O",
        "qcow2",
        str(local_chain[-1]),
        str(output),
    )
    info = json.loads(_run(qemu_img, "info", "--output=json", str(output)).stdout)
    assert info["format"] == "qcow2"
    assert not info.get("backing-filename")
    _run(qemu_img, "check", str(output))
    return output


def _boot_and_read_markers(
    disk: Path,
    root: Path,
    socket_dir: Path,
    source_config: VMConfig,
    ssh_key_path: str | None,
) -> tuple[str, bool]:
    config = source_config.model_copy(
        update={
            "vm_id": f"sbx-phase0-{disk.stem}",
            "rootfs_path": disk,
            "disk_mode": "shared",
            "disk_size_mib": None,
        }
    )
    vm = SmolVM(
        config=config,
        data_dir=root,
        socket_dir=socket_dir,
        backend="qemu",
        ssh_key_path=ssh_key_path,
        comm_channel="ssh",
        ssh_user="root",
    )
    try:
        vm.start(boot_timeout=180)
        marker_a = vm.run("cat /root/celesto-marker-a").stdout.strip()
        marker_b_exists = vm.run("test -e /root/celesto-marker-b").exit_code == 0
        return marker_a, marker_b_exists
    finally:
        with suppress(Exception):
            vm.stop(timeout=10)
        with suppress(Exception):
            vm.delete()


def test_materialized_incremental_leaves_boot_after_source_state_is_deleted(
    tmp_path: Path,
) -> None:
    """Boot base+A and base+A+B after deleting the source VM and managed disk."""
    qemu_img = shutil.which("qemu-img")
    if qemu_img is None or (
        shutil.which("qemu-system-x86_64") is None and shutil.which("qemu-system-aarch64") is None
    ):
        pytest.skip("qemu-system and qemu-img are required")

    source_data = tmp_path / "source-data"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    bitmap_name = "celesto-phase0-boot"
    base = artifacts / "base.qcow2"
    increment_a = artifacts / "increment-a.qcow2"
    increment_b = artifacts / "increment-b.qcow2"

    with tempfile.TemporaryDirectory(prefix="qinc-src-") as source_socket_raw:
        source_socket_dir = Path(source_socket_raw)
        source = SmolVM(
            backend="qemu",
            os="ubuntu",
            data_dir=source_data,
            socket_dir=source_socket_dir,
            disk_size=4096,
            comm_channel="ssh",
        )
        source_disk: Path | None = None
        source_config: VMConfig | None = None
        ssh_key_path: str | None = None
        try:
            source.start(boot_timeout=180)
            assert source.run("printf base > /root/celesto-marker-base && sync").exit_code == 0
            source_disk = source._info.config.rootfs_path
            source_config = source._info.config
            ssh_key_path = source._ssh_key_path
            base_snapshot = source.snapshot(
                snapshot_id="snap-phase0-base",
                snapshot_type=SnapshotType.DISK,
                resume_source=True,
                capture_policy=SnapshotCapturePolicy.LIVE_ONLY,
                qemu_dirty_bitmap_backup=QemuDirtyBitmapBackup(
                    mode="new-base",
                    bitmap_name=bitmap_name,
                ),
            )
            assert base_snapshot.artifact_kind == "full"
            assert base_snapshot.bitmap_name == bitmap_name
            shutil.copy2(base_snapshot.artifacts.disk_path, base)

            assert source.run("printf marker-a > /root/celesto-marker-a && sync").exit_code == 0
            snapshot_a = source.snapshot(
                snapshot_id="snap-phase0-a",
                snapshot_type=SnapshotType.DISK,
                resume_source=True,
                capture_policy=SnapshotCapturePolicy.LIVE_ONLY,
                qemu_dirty_bitmap_backup=QemuDirtyBitmapBackup(
                    mode="incremental",
                    bitmap_name=bitmap_name,
                ),
            )
            assert snapshot_a.artifact_kind == "incremental"
            assert snapshot_a.changed_bytes is not None and snapshot_a.changed_bytes > 0
            shutil.copy2(snapshot_a.artifacts.disk_path, increment_a)

            assert source.run("printf marker-b > /root/celesto-marker-b && sync").exit_code == 0
            snapshot_b = source.snapshot(
                snapshot_id="snap-phase0-b",
                snapshot_type=SnapshotType.DISK,
                resume_source=True,
                capture_policy=SnapshotCapturePolicy.LIVE_ONLY,
                qemu_dirty_bitmap_backup=QemuDirtyBitmapBackup(
                    mode="incremental",
                    bitmap_name=bitmap_name,
                ),
            )
            assert snapshot_b.artifact_kind == "incremental"
            shutil.copy2(snapshot_b.artifacts.disk_path, increment_b)
        finally:
            with suppress(Exception):
                source.stop(timeout=10)
            with suppress(Exception):
                source.delete()

    assert source_disk is not None
    assert source_config is not None
    assert not source_disk.exists()
    shutil.rmtree(source_data, ignore_errors=True)

    leaf_a = _materialize(qemu_img, tmp_path, "a", [base, increment_a])
    leaf_b = _materialize(qemu_img, tmp_path, "b", [base, increment_a, increment_b])

    with tempfile.TemporaryDirectory(prefix="qinc-a-") as socket_a_raw:
        marker_a, marker_b_exists = _boot_and_read_markers(
            leaf_a,
            tmp_path / "restore-a",
            Path(socket_a_raw),
            source_config,
            ssh_key_path,
        )
    assert marker_a == "marker-a"
    assert not marker_b_exists

    with tempfile.TemporaryDirectory(prefix="qinc-b-") as socket_b_raw:
        marker_a, marker_b_exists = _boot_and_read_markers(
            leaf_b,
            tmp_path / "restore-b",
            Path(socket_b_raw),
            source_config,
            ssh_key_path,
        )
    assert marker_a == "marker-a"
    assert marker_b_exists
