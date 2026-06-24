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


def _native() -> MagicMock:
    native = MagicMock()
    native.has_native_firecracker_api.return_value = True
    native._firecracker_request.return_value = (204, None)
    native._firecracker_wait_for_socket.return_value = None
    return native


def _client(tmp_path: Path) -> FirecrackerClient:
    return FirecrackerClient(tmp_path / "fc.sock")


def _body(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


def test_pause_resume_vm_payloads(tmp_path: Path) -> None:
    """Pause and resume should use PATCH /vm with the expected state values."""
    native = _native()
    client = _client(tmp_path)

    with patch("smolvm.api._native", native):
        client.pause_vm()
        client.resume_vm()

    requests = native._firecracker_request.call_args_list
    assert requests[0].args == (
        str(client.socket_path),
        "PATCH",
        "/vm",
        _body({"state": "Paused"}),
        10.0,
    )
    assert requests[1].args == (
        str(client.socket_path),
        "PATCH",
        "/vm",
        _body({"state": "Resumed"}),
        10.0,
    )


def test_create_snapshot_payload(tmp_path: Path) -> None:
    """Snapshot create should hit the Firecracker snapshot endpoint."""
    native = _native()
    client = _client(tmp_path)
    snapshot_path = tmp_path / "vmstate.bin"
    mem_path = tmp_path / "mem.bin"

    with patch("smolvm.api._native", native):
        client.create_snapshot(snapshot_path, mem_path)

    native._firecracker_request.assert_called_once_with(
        str(client.socket_path),
        "PUT",
        "/snapshot/create",
        _body(
            {
                "snapshot_path": str(snapshot_path),
                "mem_file_path": str(mem_path),
                "snapshot_type": "Full",
            }
        ),
        10.0,
    )


def test_load_snapshot_payload(tmp_path: Path) -> None:
    """Snapshot load should use mem_backend and preserve resume options."""
    native = _native()
    client = _client(tmp_path)
    snapshot_path = tmp_path / "vmstate.bin"
    mem_path = tmp_path / "mem.bin"

    with patch("smolvm.api._native", native):
        client.load_snapshot(
            snapshot_path,
            mem_path,
            resume_vm=True,
            network_overrides=[{"iface_id": "eth0", "host_dev_name": "tap-restored"}],
        )

    native._firecracker_request.assert_called_once_with(
        str(client.socket_path),
        "PUT",
        "/snapshot/load",
        _body(
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
        10.0,
    )


def test_firecracker_request_returns_native_json_payload(tmp_path: Path) -> None:
    """Native Firecracker helper should decode JSON object responses."""
    native = _native()
    native._firecracker_request.return_value = (200, '{"ok":true}')
    client = _client(tmp_path)

    with patch("smolvm.api._native", native):
        result = client._request("GET", "/", expected_status=(200,))

    assert result == {"ok": True}
    native._firecracker_request.assert_called_once_with(
        str(client.socket_path),
        "GET",
        "/",
        None,
        10.0,
    )


def test_firecracker_transport_error_no_longer_falls_back_to_requests(tmp_path: Path) -> None:
    """Transport errors should surface instead of using the removed Python transport."""
    native = _native()
    native._firecracker_request.side_effect = OSError("connection reset")
    client = _client(tmp_path)

    with (
        patch("smolvm.api._native", native),
        pytest.raises(FirecrackerAPIError, match="connection reset"),
    ):
        client._request("GET", "/", expected_status=(200,))

    native._firecracker_request.assert_called_once()


def test_firecracker_start_transport_error_treats_replayed_start_as_success(
    tmp_path: Path,
) -> None:
    """A native start may take effect even when its response read fails."""
    native = _native()
    native._firecracker_request.side_effect = [
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

    with patch("smolvm.api._native", native):
        client.start_instance()

    assert native._firecracker_request.call_count == 2


def test_firecracker_request_disabled_native_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMOLVM_DISABLE_NATIVE_FIRECRACKER_API should fail instead of falling back."""
    native = _native()
    client = _client(tmp_path)
    monkeypatch.setenv("SMOLVM_DISABLE_NATIVE_FIRECRACKER_API", "1")

    with (
        patch("smolvm.api._native", native),
        pytest.raises(FirecrackerAPIError, match="unset `SMOLVM_DISABLE_NATIVE_FIRECRACKER_API`"),
    ):
        client.start_instance()

    native._firecracker_request.assert_not_called()


def test_firecracker_api_error_preserves_status_code(tmp_path: Path) -> None:
    """Native HTTP responses should keep the same error contract."""
    native = _native()
    native._firecracker_request.return_value = (400, '{"fault_message":"bad request"}')
    client = _client(tmp_path)

    with (
        patch("smolvm.api._native", native),
        pytest.raises(FirecrackerAPIError) as exc_info,
    ):
        client.start_instance()

    assert exc_info.value.status_code == 400
    assert "bad request" in str(exc_info.value)


def test_wait_for_socket_uses_native_full_timeout(tmp_path: Path) -> None:
    """Native socket wait should own the whole wait instead of Python polling."""
    native = _native()
    client = _client(tmp_path)

    with patch("smolvm.api._native", native):
        client.wait_for_socket(timeout=180.0)

    native._firecracker_wait_for_socket.assert_called_once_with(str(client.socket_path), 180.0)


def test_wait_for_socket_native_timeout_raises_operation_timeout(tmp_path: Path) -> None:
    """Native socket wait timeouts should preserve the public timeout error."""
    native = _native()
    native._firecracker_wait_for_socket.side_effect = OSError("timed out")
    client = _client(tmp_path)

    with (
        patch("smolvm.api._native", native),
        pytest.raises(OperationTimeoutError),
    ):
        client.wait_for_socket(timeout=0.5)

    native._firecracker_wait_for_socket.assert_called_once_with(str(client.socket_path), 0.5)
