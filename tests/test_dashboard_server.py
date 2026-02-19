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
