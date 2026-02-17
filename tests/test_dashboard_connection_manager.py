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

"""Tests for dashboard WebSocket connection manager."""

import asyncio
from collections.abc import Callable

import pytest

pytest.importorskip("fastapi")

from smolvm.dashboard.connection_manager import ConnectionManager


class FakeWebSocket:
    """Minimal WebSocket stub for broadcast tests."""

    def __init__(self, on_send: Callable[[], None] | None = None, fail: bool = False) -> None:
        self._on_send = on_send
        self._fail = fail
        self.sent: list[dict[str, object]] = []

    async def send_json(self, message: dict[str, object]) -> None:
        self.sent.append(message)
        if self._on_send is not None:
            self._on_send()
        if self._fail:
            raise RuntimeError("send failed")


def test_broadcast_uses_stable_snapshot_when_active_set_mutates() -> None:
    """Broadcast should not fail when _active mutates during send."""
    manager = ConnectionManager()
    mutation_count = 0

    def mutate_active_set() -> None:
        nonlocal mutation_count
        mutation_count += 1
        manager._active.add(FakeWebSocket())

    ws1 = FakeWebSocket(on_send=mutate_active_set)
    ws2 = FakeWebSocket(on_send=mutate_active_set)
    manager._active = {ws1, ws2}

    asyncio.run(manager.broadcast({"type": "tick"}))

    assert mutation_count == 2
    assert ws1.sent == [{"type": "tick"}]
    assert ws2.sent == [{"type": "tick"}]


def test_broadcast_removes_dead_connections() -> None:
    """Connections that fail send_json should be removed from active set."""
    manager = ConnectionManager()
    alive = FakeWebSocket()
    dead = FakeWebSocket(fail=True)
    manager._active = {alive, dead}

    asyncio.run(manager.broadcast({"type": "tick"}))

    assert alive in manager._active
    assert dead not in manager._active
