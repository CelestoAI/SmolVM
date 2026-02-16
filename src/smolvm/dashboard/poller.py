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

"""Background poller for VM state changes.

Polls the StateManager at a configurable interval and broadcasts
deltas (status changes, new VMs, deleted VMs) over WebSocket.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smolvm.dashboard.connection_manager import ConnectionManager
    from smolvm.storage import StateManager

logger = logging.getLogger(__name__)

# Default polling interval in seconds.
DEFAULT_POLL_INTERVAL = 2.0


async def poll_vm_state(
    state_manager: StateManager,
    conn_manager: ConnectionManager,
    *,
    interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """Continuously poll VM state and broadcast changes.

    Runs as an asyncio background task. Detects:
    - New VMs (spawned since last poll)
    - Deleted VMs (removed since last poll)
    - Status changes on existing VMs

    Args:
        state_manager: SmolVM's SQLite state manager.
        conn_manager: WebSocket connection manager for broadcasting.
        interval: Seconds between polls.
    """
    prev_state: dict[str, str] = {}

    logger.info("VM state poller started (interval=%.1fs).", interval)

    while True:
        try:
            # Run the blocking SQLite query in a thread to avoid
            # blocking the async event loop.
            vms = await asyncio.to_thread(state_manager.list_vms)

            current_state: dict[str, str] = {vm.vm_id: vm.status.value for vm in vms}

            # --- Detect new VMs ---
            for vm_id, status in current_state.items():
                if vm_id not in prev_state:
                    await conn_manager.broadcast(
                        {
                            "type": "vm_created",
                            "vm_id": vm_id,
                            "status": status,
                        }
                    )
                elif prev_state[vm_id] != status:
                    # --- Detect status changes ---
                    await conn_manager.broadcast(
                        {
                            "type": "vm_updated",
                            "vm_id": vm_id,
                            "status": status,
                            "previous_status": prev_state[vm_id],
                        }
                    )

            # --- Detect deleted VMs ---
            for vm_id in prev_state:
                if vm_id not in current_state:
                    await conn_manager.broadcast(
                        {
                            "type": "vm_deleted",
                            "vm_id": vm_id,
                        }
                    )

            prev_state = current_state

        except Exception:
            logger.exception("Error during VM state polling.")

        await asyncio.sleep(interval)
