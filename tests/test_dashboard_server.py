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
