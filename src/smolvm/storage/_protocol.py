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

"""State manager protocol for SmolVM storage backends."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from smolvm.types import (
    BrowserSessionConfig,
    BrowserSessionInfo,
    BrowserSessionState,
    NetworkConfig,
    SnapshotInfo,
    VMConfig,
    VMInfo,
    VMState,
)


class StateManagerProtocol(Protocol):
    """Interface for SmolVM state persistence.

    Implementations must provide atomic operations for VM lifecycle,
    IP/port allocation, snapshots, and browser sessions.
    """

    def close(self) -> None:
        """Release backend resources (e.g. connection pool).

        No-op for SQLite. Must be called for PostgreSQL to avoid
        leaking pooled connections.
        """
        ...

    # ------------------------------------------------------------------
    # VM operations
    # ------------------------------------------------------------------

    def create_vm(self, config: VMConfig) -> VMInfo:
        pass

    def get_vm(self, vm_id: str) -> VMInfo:
        pass

    def update_vm(
        self,
        vm_id: str,
        *,
        status: VMState | None = None,
        config: VMConfig | None = None,
        network: NetworkConfig | None = None,
        pid: int | None = None,
        control_socket_path: Path | None = None,
        clear_pid: bool = False,
        clear_socket_path: bool = False,
    ) -> VMInfo:
        pass

    def delete_vm(self, vm_id: str) -> None:
        pass

    def list_vms(self, status: VMState | None = None) -> list[VMInfo]:
        pass

    # ------------------------------------------------------------------
    # IP allocation
    # ------------------------------------------------------------------

    def allocate_ip(
        self,
        vm_id: str,
        tap_device: str,
        requested_ip: str | None = None,
    ) -> str:
        pass

    def release_ip(self, vm_id: str) -> None:
        pass

    def get_ip_lease(self, vm_id: str) -> tuple[str, str] | None:
        pass

    def update_ip_lease_tap(self, vm_id: str, tap_device: str) -> None:
        pass

    # ------------------------------------------------------------------
    # SSH port allocation
    # ------------------------------------------------------------------

    def reserve_ssh_port(
        self,
        vm_id: str,
        guest_port: int = 22,
        host_port: int | None = None,
        excluded_host_ports: set[int] | None = None,
    ) -> int:
        pass

    def get_ssh_port(self, vm_id: str) -> int | None:
        pass

    def release_ssh_port(self, vm_id: str) -> None:
        pass

    # ------------------------------------------------------------------
    # vsock CID allocation
    # ------------------------------------------------------------------

    def reserve_vsock_cid(self, vm_id: str, guest_cid: int | None = None) -> int:
        pass

    def get_vsock_cid(self, vm_id: str) -> int | None:
        pass

    def release_vsock_cid(self, vm_id: str) -> None:
        pass

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def create_snapshot(self, info: SnapshotInfo) -> SnapshotInfo:
        pass

    def get_snapshot(self, snapshot_id: str) -> SnapshotInfo:
        pass

    def list_snapshots(self, vm_id: str | None = None) -> list[SnapshotInfo]:
        pass

    def mark_snapshot_restored(self, snapshot_id: str, restored_vm_id: str) -> SnapshotInfo:
        pass

    def delete_snapshot(self, snapshot_id: str) -> None:
        pass

    # ------------------------------------------------------------------
    # Browser sessions
    # ------------------------------------------------------------------

    def create_browser_session(
        self,
        info: BrowserSessionInfo,
        config: BrowserSessionConfig,
    ) -> BrowserSessionInfo:
        pass

    def get_browser_session(self, session_id: str) -> BrowserSessionInfo:
        pass

    def get_browser_session_config(self, session_id: str) -> BrowserSessionConfig:
        pass

    def update_browser_session(
        self,
        session_id: str,
        *,
        status: BrowserSessionState | None = None,
        cdp_url: str | None = None,
        live_url: str | None = None,
        vnc_url: str | None = None,
        debug_port: int | None = None,
        vnc_port: int | None = None,
        profile_id: str | None = None,
        expires_at: datetime | None = None,
        artifacts_dir: Path | None = None,
        config: BrowserSessionConfig | None = None,
    ) -> BrowserSessionInfo:
        pass

    def delete_browser_session(self, session_id: str) -> None:
        pass

    def list_browser_sessions(
        self, status: BrowserSessionState | None = None
    ) -> list[BrowserSessionInfo]:
        pass

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def reconcile(self) -> list[str]:
        pass
