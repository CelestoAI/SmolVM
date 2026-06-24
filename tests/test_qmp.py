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

"""Tests for the QMP client."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from smolvm_core import errors as core_errors
from smolvm_core import qmp as core_qmp

import smolvm.qmp as qmp_module
from smolvm.exceptions import SmolVMError
from smolvm.qmp import QMPClient


@pytest.fixture
def qmp_socket_path() -> Iterator[Path]:
    """Return a short socket path so Linux AF_UNIX path limits do not hide QMP behavior."""
    with tempfile.TemporaryDirectory(prefix="qmp-") as socket_dir:
        yield Path(socket_dir) / "q.sock"


def _start_qmp_server(
    socket_path: Path,
    responses: dict[str, list[dict[str, object] | list[dict[str, object]]]],
    requests: list[dict[str, object]],
) -> threading.Thread:
    """Start a scripted Unix-socket QMP server for a single client."""

    def _serve() -> None:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            conn, _ = server.accept()
            with conn:
                reader = conn.makefile("r", encoding="utf-8")
                writer = conn.makefile("w", encoding="utf-8")
                writer.write(
                    json.dumps(
                        {
                            "QMP": {
                                "version": {
                                    "qemu": {"major": 8, "minor": 2, "micro": 0},
                                    "package": "",
                                },
                                "capabilities": [],
                            }
                        }
                    )
                )
                writer.write("\n")
                writer.flush()

                while True:
                    line = reader.readline()
                    if not line:
                        break
                    message = json.loads(line)
                    requests.append(message)
                    command = str(message["execute"])
                    response_items = responses[command].pop(0)
                    if isinstance(response_items, dict):
                        payloads = [response_items]
                    else:
                        payloads = response_items
                    for payload in payloads:
                        writer.write(json.dumps(payload))
                        writer.write("\n")
                    writer.flush()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    deadline = time.time() + 2.0
    while not socket_path.exists():
        if time.time() >= deadline:
            raise RuntimeError("Timed out starting test QMP server")
        time.sleep(0.01)
    return thread


def test_qmp_handshake_command_execution_and_job_polling(qmp_socket_path: Path) -> None:
    """QMPClient should negotiate capabilities, execute commands, and poll jobs."""
    socket_path = qmp_socket_path
    requests: list[dict[str, object]] = []
    responses: dict[str, list[dict[str, object] | list[dict[str, object]]]] = {
        "qmp_capabilities": [{"return": {}}],
        "query-status": [
            [
                {"event": "STOP", "data": {}},
                {"return": {"running": False, "status": "paused"}},
            ]
        ],
        "snapshot-save": [{"return": {}}],
        "query-jobs": [
            {
                "return": [
                    {
                        "id": "job0",
                        "type": "snapshot-save",
                        "status": "running",
                        "current-progress": 0,
                        "total-progress": 1,
                    }
                ]
            },
            {
                "return": [
                    {
                        "id": "job0",
                        "type": "snapshot-save",
                        "status": "concluded",
                        "current-progress": 1,
                        "total-progress": 1,
                    }
                ]
            },
        ],
        "job-dismiss": [{"return": {}}],
    }
    thread = _start_qmp_server(socket_path, responses, requests)

    with QMPClient(socket_path) as client:
        client.connect()
        status = client.query_status()
        client.snapshot_save("job0", "snap0", "disk0", ["disk0"])
        job = client.wait_for_job("job0", poll_interval=0.01)

    thread.join(timeout=2.0)
    if socket_path.exists():
        socket_path.unlink()

    assert status["status"] == "paused"
    assert job.job_id == "job0"
    assert job.status == "concluded"
    assert [request["execute"] for request in requests] == [
        "qmp_capabilities",
        "query-status",
        "snapshot-save",
        "query-jobs",
        "query-jobs",
        "job-dismiss",
    ]


def test_qmp_wait_for_job_raises_on_job_error(qmp_socket_path: Path) -> None:
    """wait_for_job should surface the QMP job error field."""
    socket_path = qmp_socket_path
    requests: list[dict[str, object]] = []
    responses: dict[str, list[dict[str, object] | list[dict[str, object]]]] = {
        "qmp_capabilities": [{"return": {}}],
        "snapshot-delete": [{"return": {}}],
        "query-jobs": [
            {
                "return": [
                    {
                        "id": "job1",
                        "type": "snapshot-delete",
                        "status": "concluded",
                        "current-progress": 1,
                        "total-progress": 1,
                        "error": "snapshot tag missing",
                    }
                ]
            }
        ],
    }
    thread = _start_qmp_server(socket_path, responses, requests)

    with QMPClient(socket_path) as client:
        client.connect()
        client.snapshot_delete("job1", "snap0", ["disk0"])
        with pytest.raises(SmolVMError, match="QMP job failed"):
            client.wait_for_job("job1", poll_interval=0.01)

    thread.join(timeout=2.0)
    if socket_path.exists():
        socket_path.unlink()

    assert [request["execute"] for request in requests] == [
        "qmp_capabilities",
        "snapshot-delete",
        "query-jobs",
    ]


def test_qmp_connect_accepts_read_timeout_above_connect_probe_timeout(
    qmp_socket_path: Path,
) -> None:
    """Native QMP should accept a generous read_timeout after the connect probe.

    Regression guard for the snapshot-save read TimeoutError: the connect probe
    used a ≤1s socket timeout that previously persisted for every command read,
    cutting off replies to slow commands.
    """
    socket_path = qmp_socket_path
    requests: list[dict[str, object]] = []
    responses: dict[str, list[dict[str, object] | list[dict[str, object]]]] = {
        "qmp_capabilities": [{"return": {}}],
    }
    thread = _start_qmp_server(socket_path, responses, requests)

    with QMPClient(socket_path) as client:
        client.connect(timeout=5.0, read_timeout=30.0)

    thread.join(timeout=2.0)
    if socket_path.exists():
        socket_path.unlink()


def test_qmp_blockdev_internal_snapshot_round_trips(qmp_socket_path: Path) -> None:
    """Disk-only internal snapshot create/delete issue synchronous QMP commands."""
    socket_path = qmp_socket_path
    requests: list[dict[str, object]] = []
    responses: dict[str, list[dict[str, object] | list[dict[str, object]]]] = {
        "qmp_capabilities": [{"return": {}}],
        "blockdev-snapshot-internal-sync": [{"return": {}}],
        "blockdev-snapshot-delete-internal-sync": [{"return": {}}],
    }
    thread = _start_qmp_server(socket_path, responses, requests)

    with QMPClient(socket_path) as client:
        client.connect()
        client.blockdev_snapshot_internal_sync("rootdisk0", "snap0")
        client.blockdev_snapshot_delete_internal_sync("rootdisk0", "snap0")

    thread.join(timeout=2.0)
    if socket_path.exists():
        socket_path.unlink()

    assert [request["execute"] for request in requests] == [
        "qmp_capabilities",
        "blockdev-snapshot-internal-sync",
        "blockdev-snapshot-delete-internal-sync",
    ]
    create_req = requests[1]
    assert create_req["arguments"] == {"device": "rootdisk0", "name": "snap0"}


def test_qmp_connect_can_retry_after_capabilities_handshake_failure(
    qmp_socket_path: Path,
) -> None:
    """Failed capability negotiation should not leave the client half-connected."""
    socket_path = qmp_socket_path
    failed_requests: list[dict[str, object]] = []
    failed_responses: dict[str, list[dict[str, object] | list[dict[str, object]]]] = {
        "qmp_capabilities": [
            {
                "error": {
                    "class": "GenericError",
                    "desc": "capabilities negotiation failed",
                }
            }
        ]
    }
    failed_thread = _start_qmp_server(socket_path, failed_responses, failed_requests)

    client = QMPClient(socket_path)
    with pytest.raises(SmolVMError, match="qmp_capabilities"):
        client.connect()

    failed_thread.join(timeout=2.0)
    if socket_path.exists():
        socket_path.unlink()

    recovered_requests: list[dict[str, object]] = []
    recovered_responses: dict[str, list[dict[str, object] | list[dict[str, object]]]] = {
        "qmp_capabilities": [{"return": {}}],
        "query-status": [{"return": {"running": False, "status": "paused"}}],
    }
    recovered_thread = _start_qmp_server(socket_path, recovered_responses, recovered_requests)

    with QMPClient(socket_path) as client:
        client.connect()
        status = client.query_status()

    recovered_thread.join(timeout=2.0)
    if socket_path.exists():
        socket_path.unlink()

    assert status["status"] == "paused"
    assert [request["execute"] for request in failed_requests] == ["qmp_capabilities"]
    assert [request["execute"] for request in recovered_requests] == [
        "qmp_capabilities",
        "query-status",
    ]


def test_native_qmp_client_smoke_exercises_socket_protocol(qmp_socket_path: Path) -> None:
    """The public smolvm-core QMP client should exercise the native socket protocol."""
    assert core_qmp.available()

    socket_path = qmp_socket_path
    requests: list[dict[str, object]] = []
    responses: dict[str, list[dict[str, object] | list[dict[str, object]]]] = {
        "qmp_capabilities": [{"return": {}}],
        "query-status": [
            [
                {"event": "STOP", "data": {}},
                {"return": {"running": False, "status": "paused"}},
            ]
        ],
        "snapshot-save": [{"return": {}}],
        "query-jobs": [
            {
                "return": [
                    {
                        "id": "job0",
                        "type": "snapshot-save",
                        "status": "running",
                        "current-progress": 0,
                        "total-progress": 1,
                    }
                ]
            },
            {
                "return": [
                    {
                        "id": "job0",
                        "type": "snapshot-save",
                        "status": "concluded",
                        "current-progress": 1,
                        "total-progress": 1,
                    }
                ]
            },
        ],
        "job-dismiss": [{"return": {}}],
    }
    thread = _start_qmp_server(socket_path, responses, requests)
    client = core_qmp.QMPClient(socket_path)

    try:
        client.connect(5.0, 30.0)
        status = client.execute("query-status", None)
        client.snapshot_save("job0", "snap0", "disk0", ["disk0"])
        job = client.wait_for_job("job0", timeout=1.0, poll_interval=0.01)
    finally:
        client.close()

    thread.join(timeout=2.0)
    if socket_path.exists():
        socket_path.unlink()

    assert status["status"] == "paused"
    assert job.job_id == "job0"
    assert job.status == "concluded"
    assert [request["execute"] for request in requests] == [
        "qmp_capabilities",
        "query-status",
        "snapshot-save",
        "query-jobs",
        "query-jobs",
        "job-dismiss",
    ]


def test_qmp_client_requires_native_binding(
    monkeypatch: pytest.MonkeyPatch,
    qmp_socket_path: Path,
) -> None:
    """The public QMPClient should fail clearly when the Rust binding is absent."""
    monkeypatch.setattr(
        qmp_module.core_qmp,
        "QMPClient",
        lambda _socket_path: (_ for _ in ()).throw(
            core_errors.CoreUnavailableError("missing core")
        ),
    )
    socket_path = qmp_socket_path

    with pytest.raises(SmolVMError, match="QEMU control support is missing") as exc_info:
        qmp_module.QMPClient(socket_path)

    assert exc_info.value.details == {"socket_path": str(socket_path)}


def test_qmp_native_errors_become_smolvm_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core QMP exceptions should not leak past the public SmolVM wrapper."""

    class BrokenCoreClient:
        def __init__(self, socket_path: Path) -> None:
            self.socket_path = socket_path

        def connect(self, _timeout: float, _read_timeout: float) -> None:
            raise core_errors.QMPError(
                "Timed out waiting for QMP socket",
                {"socket_path": str(self.socket_path)},
            )

    socket_path = Path("/tmp/qmp-missing.sock")
    monkeypatch.setattr(qmp_module.core_qmp, "QMPClient", BrokenCoreClient)
    client = qmp_module.QMPClient(socket_path)

    with pytest.raises(SmolVMError, match="Timed out waiting for QMP socket") as exc_info:
        client.connect()

    assert exc_info.value.details == {"socket_path": str(socket_path)}
