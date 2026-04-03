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

"""Tests for Firecracker snapshot API helpers."""

from pathlib import Path
from unittest.mock import MagicMock

from smolvm.api import FirecrackerClient


class _Response:
    def __init__(self, status_code: int = 204, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self) -> dict:
        return self._payload


def _client(tmp_path: Path) -> FirecrackerClient:
    client = FirecrackerClient(tmp_path / "fc.sock")
    client._session = MagicMock()
    client._session.request.return_value = _Response()
    return client


def test_pause_resume_vm_payloads(tmp_path: Path) -> None:
    """Pause and resume should use PATCH /vm with the expected state values."""
    client = _client(tmp_path)

    client.pause_vm()
    client.resume_vm()

    requests = client.session.request.call_args_list
    assert requests[0].args[0] == "PATCH"
    assert requests[0].args[1].endswith("/vm")
    assert requests[0].kwargs["json"] == {"state": "Paused"}
    assert requests[1].kwargs["json"] == {"state": "Resumed"}


def test_create_snapshot_payload(tmp_path: Path) -> None:
    """Snapshot create should hit the Firecracker snapshot endpoint."""
    client = _client(tmp_path)
    snapshot_path = tmp_path / "vmstate.bin"
    mem_path = tmp_path / "mem.bin"

    client.create_snapshot(snapshot_path, mem_path)

    request = client.session.request.call_args
    assert request.args[0] == "PUT"
    assert request.args[1].endswith("/snapshot/create")
    assert request.kwargs["json"] == {
        "snapshot_path": str(snapshot_path),
        "mem_file_path": str(mem_path),
        "snapshot_type": "Full",
    }


def test_load_snapshot_payload(tmp_path: Path) -> None:
    """Snapshot load should use mem_backend and preserve resume options."""
    client = _client(tmp_path)
    snapshot_path = tmp_path / "vmstate.bin"
    mem_path = tmp_path / "mem.bin"

    client.load_snapshot(
        snapshot_path,
        mem_path,
        resume_vm=True,
        network_overrides=[{"iface_id": "eth0", "host_dev_name": "tap-restored"}],
    )

    request = client.session.request.call_args
    assert request.args[0] == "PUT"
    assert request.args[1].endswith("/snapshot/load")
    assert request.kwargs["json"] == {
        "snapshot_path": str(snapshot_path),
        "mem_backend": {
            "backend_path": str(mem_path),
            "backend_type": "File",
        },
        "resume_vm": True,
        "network_overrides": [
            {"iface_id": "eth0", "host_dev_name": "tap-restored"},
        ],
    }
