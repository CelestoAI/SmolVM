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

"""Tests for Firecracker API helpers."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.api import FirecrackerClient
from smolvm.exceptions import FirecrackerAPIError, OperationTimeoutError


def _core_client() -> MagicMock:
    client = MagicMock()
    client.request_raw.return_value = (204, None)
    client.wait_for_socket.return_value = None
    return client


def _client(tmp_path: Path) -> FirecrackerClient:
    return FirecrackerClient(tmp_path / "fc.sock")


def _body(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


def test_pause_resume_vm_payloads(tmp_path: Path) -> None:
    """Pause and resume should use PATCH /vm with the expected state values."""
    core_client = _core_client()
    client = _client(tmp_path)

    with patch("smolvm.api._require_core_firecracker", return_value=core_client):
        client.pause_vm()
        client.resume_vm()

    requests = core_client.request_raw.call_args_list
    assert requests[0].args == (
        "PATCH",
        "/vm",
    )
    assert requests[0].kwargs == {"body_json": _body({"state": "Paused"}), "timeout": 10.0}
    assert requests[1].args == (
        "PATCH",
        "/vm",
    )
    assert requests[1].kwargs == {"body_json": _body({"state": "Resumed"}), "timeout": 10.0}


def test_create_snapshot_payload(tmp_path: Path) -> None:
    """Snapshot create should hit the Firecracker snapshot endpoint."""
    core_client = _core_client()
    client = _client(tmp_path)
    snapshot_path = tmp_path / "vmstate.bin"
    mem_path = tmp_path / "mem.bin"

    with patch("smolvm.api._require_core_firecracker", return_value=core_client):
        client.create_snapshot(snapshot_path, mem_path)

    core_client.request_raw.assert_called_once_with(
        "PUT",
        "/snapshot/create",
        body_json=_body(
            {
                "snapshot_path": str(snapshot_path),
                "mem_file_path": str(mem_path),
                "snapshot_type": "Full",
            }
        ),
        timeout=10.0,
    )


def test_load_snapshot_payload(tmp_path: Path) -> None:
    """Snapshot load should use mem_backend and preserve resume options."""
    core_client = _core_client()
    client = _client(tmp_path)
    snapshot_path = tmp_path / "vmstate.bin"
    mem_path = tmp_path / "mem.bin"

    with patch("smolvm.api._require_core_firecracker", return_value=core_client):
        client.load_snapshot(
            snapshot_path,
            mem_path,
            resume_vm=True,
            network_overrides=[{"iface_id": "eth0", "host_dev_name": "tap-restored"}],
        )

    core_client.request_raw.assert_called_once_with(
        "PUT",
        "/snapshot/load",
        body_json=_body(
            {
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
        ),
        timeout=10.0,
    )


def test_firecracker_request_returns_native_json_payload(tmp_path: Path) -> None:
    """Native Firecracker helper should decode JSON object responses."""
    core_client = _core_client()
    core_client.request_raw.return_value = (200, '{"ok":true}')
    client = _client(tmp_path)

    with patch("smolvm.api._require_core_firecracker", return_value=core_client):
        result = client._request("GET", "/", expected_status=(200,))

    assert result == {"ok": True}
    core_client.request_raw.assert_called_once_with(
        "GET",
        "/",
        body_json=None,
        timeout=10.0,
    )


def test_firecracker_transport_error_no_longer_falls_back_to_requests(tmp_path: Path) -> None:
    """Transport errors should surface instead of using the removed Python transport."""
    core_client = _core_client()
    core_client.request_raw.side_effect = OSError("connection reset")
    client = _client(tmp_path)

    with (
        patch("smolvm.api._require_core_firecracker", return_value=core_client),
        pytest.raises(FirecrackerAPIError, match="connection reset"),
    ):
        client._request("GET", "/", expected_status=(200,))

    core_client.request_raw.assert_called_once()


def test_firecracker_start_transport_error_treats_replayed_start_as_success(
    tmp_path: Path,
) -> None:
    """A native start may take effect even when its response read fails."""
    core_client = _core_client()
    core_client.request_raw.side_effect = [
        OSError("connection reset"),
        (
            400,
            json.dumps(
                {
                    "fault_message": (
                        "The requested operation is not supported after starting the microVM."
                    )
                }
            ),
        ),
    ]
    client = _client(tmp_path)

    with patch("smolvm.api._require_core_firecracker", return_value=core_client):
        client.start_instance()

    assert core_client.request_raw.call_count == 2


def test_firecracker_request_disabled_native_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMOLVM_DISABLE_NATIVE_FIRECRACKER_API should fail instead of falling back."""
    core_client = _core_client()
    client = _client(tmp_path)
    monkeypatch.setenv("SMOLVM_DISABLE_NATIVE_FIRECRACKER_API", "1")

    with (
        patch("smolvm.api.core_firecracker") as mock_core_firecracker,
        pytest.raises(FirecrackerAPIError, match="unset `SMOLVM_DISABLE_NATIVE_FIRECRACKER_API`"),
    ):
        mock_core_firecracker.available.return_value = True
        client.start_instance()

    core_client.request_raw.assert_not_called()


def test_firecracker_api_error_preserves_status_code(tmp_path: Path) -> None:
    """Native HTTP responses should keep the same error contract."""
    core_client = _core_client()
    core_client.request_raw.return_value = (400, '{"fault_message":"bad request"}')
    client = _client(tmp_path)

    with (
        patch("smolvm.api._require_core_firecracker", return_value=core_client),
        pytest.raises(FirecrackerAPIError) as exc_info,
    ):
        client.start_instance()

    assert exc_info.value.status_code == 400
    assert "bad request" in str(exc_info.value)


def test_wait_for_socket_uses_native_full_timeout(tmp_path: Path) -> None:
    """Native socket wait should own the whole wait instead of Python polling."""
    core_client = _core_client()
    client = _client(tmp_path)

    with patch("smolvm.api._require_core_firecracker", return_value=core_client):
        client.wait_for_socket(timeout=180.0)

    core_client.wait_for_socket.assert_called_once_with(180.0)


def test_wait_for_socket_native_timeout_raises_operation_timeout(tmp_path: Path) -> None:
    """Native socket wait timeouts should preserve the public timeout error."""
    core_client = _core_client()
    core_client.wait_for_socket.side_effect = OSError("timed out")
    client = _client(tmp_path)

    with (
        patch("smolvm.api._require_core_firecracker", return_value=core_client),
        pytest.raises(OperationTimeoutError),
    ):
        client.wait_for_socket(timeout=0.5)

    core_client.wait_for_socket.assert_called_once_with(0.5)
