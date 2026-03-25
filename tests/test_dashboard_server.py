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

"""Tests for dashboard FastAPI server logic."""

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from smolvm.dashboard import server


class DummyStateManager:
    """StateManager stub for server endpoint tests."""

    def list_vms(self, status: object = None) -> list[object]:
        return []


class DummySDK:
    """SDK stub for command endpoint tests."""


class _FakeResponse:
    """Tiny requests.Response test double."""

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


def test_latest_dashboard_release_asset_prefers_exact_tag_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asset selection should prioritize the exact <prefix><tag>.tar.gz name."""
    payload = [
        {
            "tag_name": "v0.0.4",
            "assets": [
                {
                    "name": "smolvm-dashboard-ui-v0.0.3.tar.gz",
                    "browser_download_url": "https://example.invalid/old.tar.gz",
                },
                {
                    "name": "smolvm-dashboard-ui-v0.0.4.tar.gz",
                    "browser_download_url": "https://example.invalid/new.tar.gz",
                },
            ],
        }
    ]
    monkeypatch.setattr(server.requests, "get", lambda *args, **kwargs: _FakeResponse(payload))

    tag, url = server._latest_dashboard_release_asset()

    assert tag == "v0.0.4"
    assert url == "https://example.invalid/new.tar.gz"


def test_latest_dashboard_release_asset_skips_prerelease_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default behavior should prefer stable assets over prerelease ones."""
    payload = [
        {
            "tag_name": "v0.0.5.a0",
            "prerelease": True,
            "assets": [
                {
                    "name": "smolvm-dashboard-ui-v0.0.5.a0.tar.gz",
                    "browser_download_url": "https://example.invalid/prerelease.tar.gz",
                }
            ],
        },
        {
            "tag_name": "v0.0.4",
            "prerelease": False,
            "assets": [
                {
                    "name": "smolvm-dashboard-ui-v0.0.4.tar.gz",
                    "browser_download_url": "https://example.invalid/stable.tar.gz",
                }
            ],
        },
    ]
    monkeypatch.setattr(server.requests, "get", lambda *args, **kwargs: _FakeResponse(payload))

    tag, url = server._latest_dashboard_release_asset()

    assert tag == "v0.0.4"
    assert url == "https://example.invalid/stable.tar.gz"


def test_latest_dashboard_release_asset_allows_prerelease_with_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """allow_prerelease=True should allow selecting prerelease assets."""
    payload = [
        {
            "tag_name": "v0.0.5.a0",
            "prerelease": True,
            "assets": [
                {
                    "name": "smolvm-dashboard-ui-v0.0.5.a0.tar.gz",
                    "browser_download_url": "https://example.invalid/prerelease.tar.gz",
                }
            ],
        },
        {
            "tag_name": "v0.0.4",
            "prerelease": False,
            "assets": [
                {
                    "name": "smolvm-dashboard-ui-v0.0.4.tar.gz",
                    "browser_download_url": "https://example.invalid/stable.tar.gz",
                }
            ],
        },
    ]
    monkeypatch.setattr(server.requests, "get", lambda *args, **kwargs: _FakeResponse(payload))

    tag, url = server._latest_dashboard_release_asset(allow_prerelease=True)

    assert tag == "v0.0.5.a0"
    assert url == "https://example.invalid/prerelease.tar.gz"


def test_resolve_ui_dist_path_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """SMOLVM_DASHBOARD_UI_DIST should override default dist path resolution."""
    custom = Path("/tmp/custom-ui-dist")
    monkeypatch.setenv(server.UI_DIST_ENV, str(custom))

    assert server._resolve_ui_dist_path() == custom.resolve()


def test_allow_beta_releases_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """ALLOW_BETA_ENV should support common truthy/falsey values."""
    monkeypatch.setenv(server.ALLOW_BETA_ENV, "true")
    assert server._allow_beta_releases() is True

    monkeypatch.setenv(server.ALLOW_BETA_ENV, "0")
    assert server._allow_beta_releases() is False


def test_resolve_ui_dist_path_uses_state_dir_when_repo_layout_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Installed-package layout should fallback to resolve_data_dir()."""
    fake_server = tmp_path / "site-packages" / "smolvm" / "dashboard" / "server.py"
    fake_server.parent.mkdir(parents=True)
    fake_server.write_text("", encoding="utf-8")

    state_dir = tmp_path / "state"

    monkeypatch.delenv(server.UI_DIST_ENV, raising=False)
    monkeypatch.setattr(server, "__file__", str(fake_server))
    monkeypatch.setattr(server, "resolve_data_dir", lambda: state_dir)

    assert server._resolve_ui_dist_path() == state_dir / "dashboard-ui" / "dist"


def test_list_vms_invalid_status_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown status query values should map to 400, not 500."""
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: DummyStateManager())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.list_vms(status="paused"))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid status: paused"


def test_execute_command_route_uses_command_response_model() -> None:
    """POST /api/command should declare CommandResponse as its response model."""
    route = next(
        r
        for r in server.app.routes
        if isinstance(r, APIRoute) and r.path == "/api/command" and "POST" in r.methods
    )

    assert route.response_model is server.CommandResponse


def test_execute_command_returns_pydantic_model_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful command execution should return a validated CommandResponse."""
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: DummyStateManager())
    monkeypatch.setattr(server, "_get_sdk", lambda _app: DummySDK())

    response = asyncio.run(server.execute_command(server.CommandRequest(text="list")))

    assert isinstance(response, server.CommandResponse)
    assert response.action == "list"
    assert response.result == "Found 0 VMs."
    assert response.affected_vms == []


def test_execute_command_unknown_input_returns_400_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown command input should still return a 400 JSON error payload."""
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: DummyStateManager())
    monkeypatch.setattr(server, "_get_sdk", lambda _app: DummySDK())

    response = asyncio.run(server.execute_command(server.CommandRequest(text="nope")))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 400


# ── Tests for GET /api/vms/{vm_id}/processes ──


class _DummyNetwork:
    guest_ip = "172.16.0.2"
    gateway_ip = "172.16.0.1"
    tap_device = "tap0"
    ssh_host_port = 2201


class _DummyVMInfo:
    vm_id = "vm-test01"
    pid = 1234

    def __init__(self, *, status: object, network: object = None) -> None:
        self.status = status
        self.network = network


class _VMStateManagerStub:
    """StateManager stub that returns a configurable VMInfo for get_vm."""

    def __init__(self, vm: object | None = None) -> None:
        self._vm = vm

    def get_vm(self, vm_id: str) -> object:
        if self._vm is None:
            from smolvm.exceptions import VMNotFoundError

            raise VMNotFoundError(vm_id)
        return self._vm

    def list_vms(self, status: object = None) -> list[object]:
        return [self._vm] if self._vm else []


def test_get_vm_processes_vm_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process endpoint should return 404 for unknown VM IDs."""
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _VMStateManagerStub())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.get_vm_processes("vm-nonexistent"))

    assert exc_info.value.status_code == 404


def test_get_vm_processes_vm_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process endpoint should return 409 when the VM is stopped."""
    from smolvm.types import VMState

    vm = _DummyVMInfo(status=VMState.STOPPED, network=_DummyNetwork())
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _VMStateManagerStub(vm))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.get_vm_processes("vm-test01"))

    assert exc_info.value.status_code == 409
    assert "not running" in exc_info.value.detail


def test_get_vm_processes_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process endpoint should return 409 when the VM has no network."""
    from smolvm.types import VMState

    vm = _DummyVMInfo(status=VMState.RUNNING, network=None)
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _VMStateManagerStub(vm))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.get_vm_processes("vm-test01"))

    assert exc_info.value.status_code == 409
    assert "no network" in exc_info.value.detail


def test_get_vm_processes_parses_ps_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process endpoint should parse structured ps output correctly."""
    from unittest.mock import MagicMock, patch

    from smolvm.types import VMState

    vm = _DummyVMInfo(status=VMState.RUNNING, network=_DummyNetwork())
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _VMStateManagerStub(vm))
    monkeypatch.setattr(server, "_resolve_ssh_key_path", lambda: None)

    ps_output = (
        "  PID USER       VSZ STAT COMMAND\n"
        "    1 root      1636 S    /sbin/init\n"
        "   42 root      1508 S    /usr/sbin/sshd\n"
        "  100 root      1440 R    ps -eo pid,user,vsz,stat,args\n"
    )

    mock_ssh = MagicMock()
    mock_ssh.run.return_value = MagicMock(exit_code=0, stdout=ps_output, stderr="")

    with patch("smolvm.ssh.SSHClient", return_value=mock_ssh):
        result = asyncio.run(server.get_vm_processes("vm-test01"))

    assert result["vm_id"] == "vm-test01"
    assert len(result["processes"]) == 3
    assert result["processes"][0]["pid"] == "1"
    assert result["processes"][0]["user"] == "root"
    assert result["processes"][0]["command"] == "/sbin/init"
    assert result["processes"][1]["pid"] == "42"
    assert result["processes"][1]["stat"] == "S"


def test_get_vm_processes_ssh_failure_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process endpoint should return 502 when SSH connection fails."""
    from unittest.mock import patch

    from smolvm.exceptions import SmolVMError
    from smolvm.types import VMState

    vm = _DummyVMInfo(status=VMState.RUNNING, network=_DummyNetwork())
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _VMStateManagerStub(vm))
    monkeypatch.setattr(server, "_resolve_ssh_key_path", lambda: None)

    with (
        patch("smolvm.ssh.SSHClient", side_effect=SmolVMError("Connection refused")),
        pytest.raises(HTTPException) as exc_info,
    ):
        asyncio.run(server.get_vm_processes("vm-test01"))

    assert exc_info.value.status_code == 502


def test_resolve_ssh_key_path_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """_resolve_ssh_key_path should return None when no key files exist."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert server._resolve_ssh_key_path() is None


def test_resolve_ssh_key_path_finds_keys_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """_resolve_ssh_key_path should find keys in ~/.smolvm/keys/."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    key_path = tmp_path / ".smolvm" / "keys" / "id_ed25519"
    key_path.parent.mkdir(parents=True)
    key_path.write_text("dummy-key")

    assert server._resolve_ssh_key_path() == str(key_path)
