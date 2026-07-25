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

"""Real-QEMU proof for persistent dirty-bitmap incremental artifacts."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.qmp import QMPClient, QMPDirtyBitmap
from tests.qemu_incremental import _materialize

_VIRTUAL_SIZE = 64 * 1024 * 1024
_BITMAP_NAME = "celesto-phase0"


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """Keep QMP and NBD paths below platform AF_UNIX limits."""
    with tempfile.TemporaryDirectory(prefix="qinc-") as socket_dir:
        yield Path(socket_dir)


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


def _start_qemu(qemu_system: str, source: Path, socket_path: Path) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            qemu_system,
            "-machine",
            "none",
            "-nodefaults",
            "-display",
            "none",
            "-S",
            "-qmp",
            f"unix:{socket_path},server=on,wait=off",
            "-blockdev",
            f"driver=qcow2,node-name=rootdisk0,file.driver=file,file.filename={source}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not socket_path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if not socket_path.exists():
        if process.poll() is None:
            process.terminate()
        _, stderr = process.communicate(timeout=5)
        pytest.fail(f"QEMU did not create its QMP socket: {stderr}")
    return process


def _stop_qemu(client: QMPClient, process: subprocess.Popen[str]) -> None:
    client.execute("quit")
    process.communicate(timeout=5)


def _bitmap(client: QMPClient) -> QMPDirtyBitmap:
    bitmap = next(
        (value for value in client.query_dirty_bitmaps("rootdisk0") if value.name == _BITMAP_NAME),
        None,
    )
    if bitmap is None:
        pytest.fail(f"QEMU did not report dirty bitmap '{_BITMAP_NAME}'")
    return bitmap


def _create_target(qemu_img: str, root: Path, name: str) -> Path:
    target = root / f"{name}.qcow2"
    _run(qemu_img, "create", "-f", "qcow2", str(target), str(_VIRTUAL_SIZE))
    return target


def _attach_target(client: QMPClient, target: Path, name: str) -> str:
    node_name = f"target-{name}"
    client.blockdev_add(node_name, target)
    return node_name


def _write_pattern(qemu_io: str, nbd_socket: Path, pattern: str, offset: int, length: int) -> None:
    _run(
        qemu_io,
        "-f",
        "raw",
        "-c",
        f"write -P {pattern} {offset} {length}",
        f"nbd+unix:///source?socket={nbd_socket}",
    )


def _assert_pattern(qemu_io: str, disk: Path, pattern: str, offset: int, length: int) -> None:
    _run(qemu_io, "-f", "qcow2", "-c", f"read -P {pattern} {offset} {length}", str(disk))


def test_incremental_chain_materializes_and_cancelled_backup_retains_dirty_bytes(
    tmp_path: Path,
    short_socket_dir: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Prove base/A/B materialization, persistence, and cancellation safety."""
    qemu_img = shutil.which("qemu-img")
    qemu_io = shutil.which("qemu-io")
    qemu_system = shutil.which("qemu-system-x86_64") or shutil.which("qemu-system-aarch64")
    if qemu_img is None or qemu_io is None or qemu_system is None:
        pytest.skip("qemu-system, qemu-img, and qemu-io are required")

    source = tmp_path / "source.qcow2"
    qmp_socket = short_socket_dir / "qmp.sock"
    nbd_socket = short_socket_dir / "nbd.sock"
    _run(qemu_img, "create", "-f", "qcow2", str(source), str(_VIRTUAL_SIZE))

    artifacts: dict[str, Path] = {}
    durations: dict[str, float] = {}
    process = _start_qemu(qemu_system, source, qmp_socket)
    try:
        with QMPClient(qmp_socket) as client:
            client.connect()
            version = client.query_version()
            client.execute(
                "nbd-server-start",
                {"addr": {"type": "unix", "data": {"path": str(nbd_socket)}}},
            )
            client.execute(
                "block-export-add",
                {
                    "type": "nbd",
                    "id": "source-export",
                    "node-name": "rootdisk0",
                    "name": "source",
                    "writable": True,
                },
            )

            artifacts["base"] = _create_target(qemu_img, tmp_path, "base")
            target_node = _attach_target(client, artifacts["base"], "base")
            started = time.monotonic()
            client.start_full_backup_with_dirty_bitmap(
                job_id="job-base",
                source_node="rootdisk0",
                target_node=target_node,
                bitmap_name=_BITMAP_NAME,
            )
            client.wait_for_job("job-base", timeout=10)
            durations["base"] = time.monotonic() - started
            client.blockdev_del(target_node)
            base_bitmap = _bitmap(client)
            assert base_bitmap.recording
            assert base_bitmap.persistent
            assert not base_bitmap.busy
            assert not base_bitmap.inconsistent
            assert base_bitmap.dirty_bytes == 0
            assert base_bitmap.granularity == 64 * 1024

            _write_pattern(qemu_io, nbd_socket, "0x41", 1024 * 1024, 65536)
            assert _bitmap(client).dirty_bytes == 65536
            artifacts["a"] = _create_target(qemu_img, tmp_path, "increment-a")
            target_node = _attach_target(client, artifacts["a"], "a")
            started = time.monotonic()
            client.blockdev_backup_incremental(
                job_id="job-a",
                source_node="rootdisk0",
                target_node=target_node,
                bitmap_name=_BITMAP_NAME,
            )
            client.wait_for_job("job-a", timeout=10)
            durations["a"] = time.monotonic() - started
            client.blockdev_del(target_node)
            assert _bitmap(client).dirty_bytes == 0

            _write_pattern(qemu_io, nbd_socket, "0x42", 2 * 1024 * 1024, 65536)
            artifacts["b"] = _create_target(qemu_img, tmp_path, "increment-b")
            target_node = _attach_target(client, artifacts["b"], "b")
            started = time.monotonic()
            client.blockdev_backup_incremental(
                job_id="job-b",
                source_node="rootdisk0",
                target_node=target_node,
                bitmap_name=_BITMAP_NAME,
            )
            client.wait_for_job("job-b", timeout=10)
            durations["b"] = time.monotonic() - started
            client.blockdev_del(target_node)
            assert _bitmap(client).dirty_bytes == 0

            client.execute("block-export-del", {"id": "source-export", "mode": "hard"})
            client.execute("nbd-server-stop")
            _stop_qemu(client, process)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)

    qmp_socket.unlink(missing_ok=True)
    process = _start_qemu(qemu_system, source, qmp_socket)
    try:
        with QMPClient(qmp_socket) as client:
            client.connect()
            persisted = _bitmap(client)
            assert persisted.recording
            assert persisted.persistent
            assert not persisted.inconsistent
            assert persisted.dirty_bytes == 0
            client.execute(
                "nbd-server-start",
                {"addr": {"type": "unix", "data": {"path": str(nbd_socket)}}},
            )
            client.execute(
                "block-export-add",
                {
                    "type": "nbd",
                    "id": "source-export",
                    "node-name": "rootdisk0",
                    "name": "source",
                    "writable": True,
                },
            )

            cancelled_bytes = 16 * 1024 * 1024
            _write_pattern(qemu_io, nbd_socket, "0x43", 4 * 1024 * 1024, cancelled_bytes)
            assert _bitmap(client).dirty_bytes == cancelled_bytes
            cancelled = _create_target(qemu_img, tmp_path, "cancelled")
            target_node = _attach_target(client, cancelled, "cancelled")
            client.blockdev_backup_incremental(
                job_id="job-cancelled",
                source_node="rootdisk0",
                target_node=target_node,
                bitmap_name=_BITMAP_NAME,
                max_bytes_per_second=1024 * 1024,
            )
            time.sleep(0.2)
            client.cancel_job("job-cancelled", force=True)
            with pytest.raises(SmolVMError, match="QMP job failed"):
                client.wait_for_job("job-cancelled", timeout=10)
            client.blockdev_del(target_node)
            assert _bitmap(client).dirty_bytes == cancelled_bytes

            artifacts["retry"] = _create_target(qemu_img, tmp_path, "increment-retry")
            target_node = _attach_target(client, artifacts["retry"], "retry")
            client.blockdev_backup_incremental(
                job_id="job-retry",
                source_node="rootdisk0",
                target_node=target_node,
                bitmap_name=_BITMAP_NAME,
            )
            client.wait_for_job("job-retry", timeout=10)
            client.blockdev_del(target_node)
            assert _bitmap(client).dirty_bytes == 0

            client.execute("block-export-del", {"id": "source-export", "mode": "hard"})
            client.execute("nbd-server-stop")
            _stop_qemu(client, process)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)

    leaf_a = _materialize(qemu_img, tmp_path, "a", [artifacts["base"], artifacts["a"]])
    _assert_pattern(qemu_io, leaf_a, "0x41", 1024 * 1024, 65536)
    _assert_pattern(qemu_io, leaf_a, "0x00", 2 * 1024 * 1024, 65536)

    leaf_b = _materialize(
        qemu_img,
        tmp_path,
        "b",
        [artifacts["base"], artifacts["a"], artifacts["b"]],
    )
    _assert_pattern(qemu_io, leaf_b, "0x41", 1024 * 1024, 65536)
    _assert_pattern(qemu_io, leaf_b, "0x42", 2 * 1024 * 1024, 65536)

    retry_leaf = _materialize(
        qemu_img,
        tmp_path,
        "retry",
        [artifacts["base"], artifacts["a"], artifacts["b"], artifacts["retry"]],
    )
    _assert_pattern(qemu_io, retry_leaf, "0x43", 4 * 1024 * 1024, 16 * 1024 * 1024)

    evidence = {
        "qemu_version": version,
        "bitmap_granularity": base_bitmap.granularity,
        "capture_seconds": durations,
        "artifact_actual_bytes": {
            name: json.loads(_run(qemu_img, "info", "--output=json", str(path)).stdout)[
                "actual-size"
            ]
            for name, path in artifacts.items()
        },
    }
    serialized_evidence = json.dumps(evidence, sort_keys=True)
    request.node.user_properties.append(("qemu_incremental_evidence", serialized_evidence))
    print(serialized_evidence)
