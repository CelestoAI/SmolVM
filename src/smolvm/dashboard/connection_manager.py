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

"""WebSocket connection manager for real-time VM state streaming."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts VM state updates.

    Thread-safe broadcast with automatic cleanup of dead connections.
    """

    def __init__(self) -> None:
        self._active: set[WebSocket] = set()

    @property
    def connection_count(self) -> int:
        """Number of active WebSocket connections."""
        return len(self._active)

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a new WebSocket connection.

        Args:
            websocket: The incoming WebSocket connection.
        """
        await websocket.accept()
        self._active.add(websocket)
        logger.info(
            "WebSocket connected. Active connections: %d",
            self.connection_count,
        )

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection from the active set.

        Args:
            websocket: The disconnecting WebSocket.
        """
        self._active.discard(websocket)
        logger.info(
            "WebSocket disconnected. Active connections: %d",
            self.connection_count,
        )

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send a JSON message to all active connections.

        Dead connections are silently removed.

        Args:
            message: JSON-serializable dictionary to broadcast.
        """
        if not self._active:
            return

        dead: set[WebSocket] = set()
        # Iterate over a snapshot so connect/disconnect can safely mutate _active.
        for connection in tuple(self._active):
            try:
                await connection.send_json(message)
            except Exception:
                dead.add(connection)

        if dead:
            self._active -= dead
            logger.warning("Cleaned up %d dead WebSocket connections.", len(dead))

    async def send_personal(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        """Send a JSON message to a specific connection.

        Args:
            websocket: Target WebSocket connection.
            message: JSON-serializable dictionary.
        """
        try:
            await websocket.send_json(message)
        except Exception:
            self._active.discard(websocket)
