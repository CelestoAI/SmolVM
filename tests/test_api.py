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

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.api import FirecrackerClient
from smolvm.exceptions import FirecrackerAPIError


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


def test_native_firecracker_request_path_is_used_when_available(tmp_path: Path) -> None:
    """Native Firecracker helper should bypass requests when enabled and socket exists."""
    socket_path = tmp_path / "fc.sock"
    socket_path.touch()
    native = MagicMock()
    native.has_native_firecracker_api.return_value = True
    native._firecracker_request.return_value = (204, None)
    client = _client(tmp_path)

    with patch("smolvm.api._native", native):
        client.start_instance()

    native._firecracker_request.assert_called_once_with(
        str(socket_path),
        "PUT",
        "/actions",
        json.dumps({"action_type": "InstanceStart"}, separators=(",", ":")),
        10.0,
    )
    client.session.request.assert_not_called()


def test_native_firecracker_transport_error_falls_back_to_requests(tmp_path: Path) -> None:
    """Native transport errors should fall through to requests-unixsocket."""
    socket_path = tmp_path / "fc.sock"
    socket_path.touch()
    native = MagicMock()
    native.has_native_firecracker_api.return_value = True
    native._firecracker_request.side_effect = OSError("connection reset")
    client = _client(tmp_path)
    client.session.request.return_value = _Response(status_code=200, payload={"ok": True})

    with patch("smolvm.api._native", native):
        assert client._request("GET", "/", expected_status=(200,)) == {"ok": True}

    native._firecracker_request.assert_called_once()
    client.session.request.assert_called_once()


def test_native_firecracker_transport_error_treats_replayed_start_as_success(
    tmp_path: Path,
) -> None:
    """A native start may take effect even when its response read fails."""
    socket_path = tmp_path / "fc.sock"
    socket_path.touch()
    native = MagicMock()
    native.has_native_firecracker_api.return_value = True
    native._firecracker_request.side_effect = OSError("connection reset")
    client = _client(tmp_path)
    client.session.request.return_value = _Response(
        status_code=400,
        payload={
            "fault_message": (
                "The requested operation is not supported after starting the microVM."
            )
        },
    )

    with patch("smolvm.api._native", native):
        client.start_instance()

    native._firecracker_request.assert_called_once()
    client.session.request.assert_called_once()


def test_native_firecracker_request_can_be_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMOLVM_DISABLE_NATIVE_FIRECRACKER_API should force the requests path."""
    socket_path = tmp_path / "fc.sock"
    socket_path.touch()
    native = MagicMock()
    native.has_native_firecracker_api.return_value = True
    client = _client(tmp_path)
    monkeypatch.setenv("SMOLVM_DISABLE_NATIVE_FIRECRACKER_API", "1")

    with patch("smolvm.api._native", native):
        client.start_instance()

    native._firecracker_request.assert_not_called()
    client.session.request.assert_called_once()


def test_native_firecracker_api_error_preserves_status_code(tmp_path: Path) -> None:
    """Native HTTP responses should keep the same error contract as requests."""
    socket_path = tmp_path / "fc.sock"
    socket_path.touch()
    native = MagicMock()
    native.has_native_firecracker_api.return_value = True
    native._firecracker_request.return_value = (400, '{"fault_message":"bad request"}')
    client = _client(tmp_path)

    with (
        patch("smolvm.api._native", native),
        pytest.raises(FirecrackerAPIError) as exc_info,
    ):
        client.start_instance()

    assert exc_info.value.status_code == 400
    assert "bad request" in str(exc_info.value)
    client.session.request.assert_not_called()


def test_wait_for_socket_native_timeout_falls_back_to_polling(tmp_path: Path) -> None:
    """Native socket wait timeout should still try the Python polling loop."""
    socket_path = tmp_path / "fc.sock"
    socket_path.touch()
    native = MagicMock()
    native.has_native_firecracker_api.return_value = True
    native._firecracker_wait_for_socket.side_effect = OSError("timed out")
    client = FirecrackerClient(socket_path)
    client._request = MagicMock()

    with patch("smolvm.api._native", native):
        client.wait_for_socket(timeout=0.5)

    native._firecracker_wait_for_socket.assert_called_once_with(str(socket_path), 0.5)
    client._request.assert_called_once_with("GET", "/", expected_status=(200,))


def test_wait_for_socket_caps_native_probe_before_polling(tmp_path: Path) -> None:
    """A native wait failure should not consume the full boot timeout."""
    socket_path = tmp_path / "fc.sock"
    socket_path.touch()
    native = MagicMock()
    native.has_native_firecracker_api.return_value = True
    native._firecracker_wait_for_socket.side_effect = OSError("timed out")
    client = FirecrackerClient(socket_path)
    client._request = MagicMock()

    with patch("smolvm.api._native", native):
        client.wait_for_socket(timeout=180.0)

    native._firecracker_wait_for_socket.assert_called_once_with(str(socket_path), 1.0)
    client._request.assert_called_once_with("GET", "/", expected_status=(200,))
