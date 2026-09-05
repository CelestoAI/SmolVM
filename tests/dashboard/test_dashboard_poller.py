"""Tests for dashboard VM-state change detection."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

from smolvm.dashboard.poller import poll_vm_state
from smolvm.types import VMState


class FakeStateManager:
    def __init__(self, snapshots: list[list[tuple[str, VMState]]]) -> None:
        self._snapshots = iter(snapshots)

    def list_vms(self) -> list[Any]:
        snapshot = next(self._snapshots)
        return [SimpleNamespace(vm_id=vm_id, status=status) for vm_id, status in snapshot]


class FakeConnectionManager:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    async def broadcast(self, message: dict[str, str]) -> None:
        self.messages.append(message)


def test_poller_broadcasts_created_updated_and_deleted_vms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = FakeStateManager(
        [
            [("one", VMState.RUNNING), ("two", VMState.STOPPED)],
            [("one", VMState.PAUSED), ("three", VMState.CREATED)],
        ]
    )
    connections = FakeConnectionManager()
    calls = 0

    async def stop_after_two_polls(_interval: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("smolvm.dashboard.poller.asyncio.sleep", stop_after_two_polls)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(poll_vm_state(state, connections, interval=0))

    assert connections.messages == [
        {"type": "vm_created", "vm_id": "one", "status": "running"},
        {"type": "vm_created", "vm_id": "two", "status": "stopped"},
        {
            "type": "vm_updated",
            "vm_id": "one",
            "status": "paused",
            "previous_status": "running",
        },
        {"type": "vm_created", "vm_id": "three", "status": "created"},
        {"type": "vm_deleted", "vm_id": "two"},
    ]


def test_poller_logs_state_errors_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenState:
        def list_vms(self) -> list[Any]:
            raise RuntimeError("state unavailable")

    connections = FakeConnectionManager()
    sleep_calls = 0

    async def stop_after_error(_interval: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        raise asyncio.CancelledError

    monkeypatch.setattr("smolvm.dashboard.poller.asyncio.sleep", stop_after_error)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(poll_vm_state(BrokenState(), connections, interval=0))

    assert sleep_calls == 1
    assert connections.messages == []
