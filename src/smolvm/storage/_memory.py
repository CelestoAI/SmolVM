# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Process-local state used by the Python SDK and HTTP API.

This implementation deliberately has no database dependency. It is a migration
seam while VM lifecycle state moves fully onto the ``SmolVM`` handle; persistent
inventory belongs to the CLI. Advisory resource locks live in the caller's
SmolVM data directory.
"""

from __future__ import annotations

import os
import secrets
import socket
import threading
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from smolvm.exceptions import (
    BrowserSessionAlreadyExistsError,
    BrowserSessionNotFoundError,
    NetworkError,
    SnapshotAlreadyExistsError,
    SnapshotNotFoundError,
    VMAlreadyExistsError,
    VMNotFoundError,
)
from smolvm.storage._base import (
    IP_POOL_END,
    IP_POOL_START,
    SSH_PORT_END,
    SSH_PORT_START,
    VSOCK_CID_END,
    VSOCK_CID_START,
    pool_index_to_ip,
)
from smolvm.types import (
    BrowserSessionConfig,
    BrowserSessionInfo,
    BrowserSessionState,
    DesktopEndpoint,
    NetworkConfig,
    SnapshotInfo,
    VMConfig,
    VMInfo,
    VMState,
)


class MemoryStateManager:
    """Thread-safe, process-local implementation of the state protocol."""

    def __init__(self, data_dir: Path | None = None) -> None:
        if data_dir is None:
            raise ValueError("data_dir is required for secure resource locks")
        self._lock = threading.RLock()
        self._lock_dir = data_dir / "locks" / "resources"
        self._claims: dict[tuple[str, str], BinaryIO] = {}
        self._vms: dict[str, VMInfo] = {}
        self._ip_leases: dict[str, tuple[str, str]] = {}
        self._ssh_ports: dict[str, int] = {}
        self._vsock_cids: dict[str, int] = {}
        self._tap_allocations: dict[str, tuple[str, str, str | None]] = {}
        self._snapshots: dict[str, SnapshotInfo] = {}
        self._browser_sessions: dict[str, BrowserSessionInfo] = {}
        self._browser_configs: dict[str, BrowserSessionConfig] = {}

    def close(self) -> None:
        """Release no VM resources; explicit VM deletion owns that lifecycle."""

    def _try_claim(self, kind: str, vm_id: str, value: str | int) -> bool:
        """Claim a resource across local processes with an advisory lock."""
        key = (kind, vm_id)
        if key in self._claims:
            return True
        try:
            import fcntl
        except ImportError:
            return True
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        claim = (self._lock_dir / f"{kind}-{value}.lock").open("a+b")
        try:
            fcntl.flock(claim.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            claim.close()
            return False
        self._claims[key] = claim
        return True

    def _release_claim(self, kind: str, vm_id: str) -> None:
        claim = self._claims.pop((kind, vm_id), None)
        if claim is not None:
            claim.close()

    def create_vm(self, config: VMConfig) -> VMInfo:
        with self._lock:
            if config.vm_id in self._vms:
                raise VMAlreadyExistsError(config.vm_id)
            info = VMInfo(vm_id=config.vm_id, status=VMState.CREATED, config=config)
            self._vms[config.vm_id] = info
            return info

    def get_vm(self, vm_id: str) -> VMInfo:
        with self._lock:
            try:
                return self._vms[vm_id]
            except KeyError:
                raise VMNotFoundError(vm_id) from None

    def update_vm(
        self,
        vm_id: str,
        *,
        status: VMState | None = None,
        config: VMConfig | None = None,
        network: NetworkConfig | None = None,
        pid: int | None = None,
        control_socket_path: Path | None = None,
        display: DesktopEndpoint | None = None,
        clear_pid: bool = False,
        clear_socket_path: bool = False,
        clear_display: bool = False,
    ) -> VMInfo:
        with self._lock:
            current = self.get_vm(vm_id)
            updates: dict[str, object] = {}
            if status is not None:
                updates["status"] = status
            if config is not None:
                updates["config"] = config
            if network is not None:
                updates["network"] = network
            if pid is not None:
                updates["pid"] = pid
            elif clear_pid:
                updates["pid"] = None
            if control_socket_path is not None:
                updates["control_socket_path"] = control_socket_path
            elif clear_socket_path:
                updates["control_socket_path"] = None
            if display is not None:
                updates["display"] = display
            elif clear_display:
                updates["display"] = None
            updated = current.model_copy(update=updates)
            self._vms[vm_id] = updated
            return updated

    def delete_vm(self, vm_id: str) -> None:
        with self._lock:
            if vm_id not in self._vms:
                raise VMNotFoundError(vm_id)
            del self._vms[vm_id]
            self._ip_leases.pop(vm_id, None)
            self._ssh_ports.pop(vm_id, None)
            self._vsock_cids.pop(vm_id, None)
            self._tap_allocations.pop(vm_id, None)
            for kind in ("ip", "ssh", "vsock", "tap"):
                self._release_claim(kind, vm_id)

    def list_vms(self, status: VMState | None = None) -> list[VMInfo]:
        with self._lock:
            values = list(self._vms.values())
            return values if status is None else [item for item in values if item.status == status]

    @staticmethod
    def _host_interface_names() -> set[str]:
        try:
            return {name for _index, name in socket.if_nameindex()}
        except OSError:
            return set()

    def allocate_ip(
        self,
        vm_id: str,
        tap_device: str,
        requested_ip: str | None = None,
    ) -> str:
        with self._lock:
            existing = self._ip_leases.get(vm_id)
            if existing is not None:
                if requested_ip is not None and requested_ip != existing[0]:
                    raise NetworkError(
                        f"VM {vm_id} already has IP {existing[0]}, cannot reserve {requested_ip}"
                    )
                return existing[0]

            used_ips = {lease[0] for lease in self._ip_leases.values()}
            host_interfaces = self._host_interface_names()
            candidates = (
                [requested_ip]
                if requested_ip is not None
                else (pool_index_to_ip(i) for i in range(IP_POOL_START, IP_POOL_END + 1))
            )
            for candidate in candidates:
                if candidate is None or candidate in used_ips:
                    continue
                parts = candidate.split(".")
                if len(parts) == 4:
                    index = (int(parts[2]) << 8) | int(parts[3])
                    if f"tap{index}" in host_interfaces:
                        continue
                if not self._try_claim("ip", vm_id, candidate):
                    continue
                self._ip_leases[vm_id] = (candidate, tap_device)
                return candidate
        if requested_ip is not None:
            raise NetworkError(f"Requested IP address {requested_ip} is not available")
        raise NetworkError("No IP addresses available in pool")

    def release_ip(self, vm_id: str) -> None:
        with self._lock:
            self._ip_leases.pop(vm_id, None)
            self._release_claim("ip", vm_id)

    def get_ip_lease(self, vm_id: str) -> tuple[str, str] | None:
        with self._lock:
            return self._ip_leases.get(vm_id)

    def update_ip_lease_tap(self, vm_id: str, tap_device: str) -> None:
        with self._lock:
            lease = self._ip_leases.get(vm_id)
            if lease is not None:
                self._ip_leases[vm_id] = (lease[0], tap_device)

    def reserve_ssh_port(
        self,
        vm_id: str,
        guest_port: int = 22,
        host_port: int | None = None,
        excluded_host_ports: set[int] | None = None,
    ) -> int:
        del guest_port
        with self._lock:
            existing = self._ssh_ports.get(vm_id)
            if existing is not None:
                if host_port is not None and host_port != existing:
                    raise NetworkError(
                        f"VM {vm_id} already has SSH host port {existing}, "
                        f"cannot reserve {host_port}"
                    )
                return existing
            used = set(self._ssh_ports.values()) | (excluded_host_ports or set())
            candidates = (
                [host_port] if host_port is not None else range(SSH_PORT_START, SSH_PORT_END + 1)
            )
            for candidate in candidates:
                if (
                    candidate is not None
                    and candidate not in used
                    and self._try_claim("ssh", vm_id, candidate)
                ):
                    self._ssh_ports[vm_id] = candidate
                    return candidate
        if host_port is not None:
            raise NetworkError(f"Requested SSH host port {host_port} is not available")
        raise NetworkError("No SSH host ports available in pool")

    def get_ssh_port(self, vm_id: str) -> int | None:
        with self._lock:
            return self._ssh_ports.get(vm_id)

    def release_ssh_port(self, vm_id: str) -> None:
        with self._lock:
            self._ssh_ports.pop(vm_id, None)
            self._release_claim("ssh", vm_id)

    def reserve_vsock_cid(self, vm_id: str, guest_cid: int | None = None) -> int:
        with self._lock:
            existing = self._vsock_cids.get(vm_id)
            if existing is not None:
                if guest_cid is not None and guest_cid != existing:
                    raise NetworkError(
                        f"VM {vm_id} already has vsock CID {existing}, cannot reserve {guest_cid}"
                    )
                return existing
            used = set(self._vsock_cids.values())
            candidates = (
                [guest_cid] if guest_cid is not None else range(VSOCK_CID_START, VSOCK_CID_END + 1)
            )
            for candidate in candidates:
                if (
                    candidate is not None
                    and candidate not in used
                    and self._try_claim("vsock", vm_id, candidate)
                ):
                    self._vsock_cids[vm_id] = candidate
                    return candidate
        if guest_cid is not None:
            raise NetworkError(f"Requested vsock CID {guest_cid} is not available")
        raise NetworkError("No vsock CIDs available in pool")

    def get_vsock_cid(self, vm_id: str) -> int | None:
        with self._lock:
            return self._vsock_cids.get(vm_id)

    def release_vsock_cid(self, vm_id: str) -> None:
        with self._lock:
            self._vsock_cids.pop(vm_id, None)
            self._release_claim("vsock", vm_id)

    def reserve_tap_name(
        self,
        vm_id: str,
        mode: str,
        bridge_name: str | None = None,
        requested_tap: str | None = None,
    ) -> str:
        with self._lock:
            existing = self._tap_allocations.get(vm_id)
            if existing is not None:
                tap, existing_mode, existing_bridge = existing
                if (
                    existing_mode != mode
                    or existing_bridge != bridge_name
                    or (requested_tap is not None and requested_tap != tap)
                ):
                    raise NetworkError(f"Sandbox '{vm_id}' already reserves TAP '{tap}'.")
                return tap
            used = {allocation[0] for allocation in self._tap_allocations.values()}
            used |= self._host_interface_names()
            if requested_tap is not None:
                if requested_tap in used:
                    raise NetworkError(f"TAP device name '{requested_tap}' is already reserved")
                tap = requested_tap
            else:
                for _ in range(100):
                    tap = f"svmb{secrets.token_hex(4)}"
                    if tap not in used:
                        break
                else:
                    raise NetworkError("Could not find a free TAP device name")
            if not self._try_claim("tap", vm_id, tap):
                raise NetworkError(f"TAP device name '{tap}' is already reserved")
            self._tap_allocations[vm_id] = (tap, mode, bridge_name)
            return tap

    def get_tap_allocation(self, vm_id: str) -> tuple[str, str, str | None] | None:
        with self._lock:
            return self._tap_allocations.get(vm_id)

    def release_tap_name(self, vm_id: str) -> None:
        with self._lock:
            self._tap_allocations.pop(vm_id, None)
            self._release_claim("tap", vm_id)

    def create_snapshot(self, info: SnapshotInfo) -> SnapshotInfo:
        with self._lock:
            if info.snapshot_id in self._snapshots:
                raise SnapshotAlreadyExistsError(info.snapshot_id)
            self._snapshots[info.snapshot_id] = info
            return info

    def get_snapshot(self, snapshot_id: str) -> SnapshotInfo:
        with self._lock:
            try:
                return self._snapshots[snapshot_id]
            except KeyError:
                raise SnapshotNotFoundError(snapshot_id) from None

    def list_snapshots(self, vm_id: str | None = None) -> list[SnapshotInfo]:
        with self._lock:
            values = list(self._snapshots.values())
            return values if vm_id is None else [item for item in values if item.vm_id == vm_id]

    def mark_snapshot_restored(self, snapshot_id: str, restored_vm_id: str) -> SnapshotInfo:
        with self._lock:
            info = self.get_snapshot(snapshot_id).model_copy(
                update={"restored": True, "restored_vm_id": restored_vm_id}
            )
            self._snapshots[snapshot_id] = info
            return info

    def delete_snapshot(self, snapshot_id: str) -> None:
        with self._lock:
            if self._snapshots.pop(snapshot_id, None) is None:
                raise SnapshotNotFoundError(snapshot_id)

    def create_browser_session(
        self, info: BrowserSessionInfo, config: BrowserSessionConfig
    ) -> BrowserSessionInfo:
        with self._lock:
            if info.session_id in self._browser_sessions:
                raise BrowserSessionAlreadyExistsError(info.session_id)
            self._browser_sessions[info.session_id] = info
            self._browser_configs[info.session_id] = config
            return info

    def get_browser_session(self, session_id: str) -> BrowserSessionInfo:
        with self._lock:
            try:
                return self._browser_sessions[session_id]
            except KeyError:
                raise BrowserSessionNotFoundError(session_id) from None

    def get_browser_session_config(self, session_id: str) -> BrowserSessionConfig:
        with self._lock:
            try:
                return self._browser_configs[session_id]
            except KeyError:
                raise BrowserSessionNotFoundError(session_id) from None

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
        with self._lock:
            current = self.get_browser_session(session_id)
            updates = {
                key: value
                for key, value in {
                    "status": status,
                    "cdp_url": cdp_url,
                    "live_url": live_url,
                    "vnc_url": vnc_url,
                    "debug_port": debug_port,
                    "vnc_port": vnc_port,
                    "profile_id": profile_id,
                    "expires_at": expires_at,
                    "artifacts_dir": artifacts_dir,
                }.items()
                if value is not None
            }
            updated = current.model_copy(update=updates)
            self._browser_sessions[session_id] = updated
            if config is not None:
                self._browser_configs[session_id] = config
            return updated

    def delete_browser_session(self, session_id: str) -> None:
        with self._lock:
            if self._browser_sessions.pop(session_id, None) is None:
                raise BrowserSessionNotFoundError(session_id)
            self._browser_configs.pop(session_id, None)

    def list_browser_sessions(
        self, status: BrowserSessionState | None = None
    ) -> list[BrowserSessionInfo]:
        with self._lock:
            values = list(self._browser_sessions.values())
            return values if status is None else [item for item in values if item.status == status]

    def reconcile(self) -> list[str]:
        stale: list[str] = []
        with self._lock:
            for vm_id, info in list(self._vms.items()):
                if info.status not in (VMState.RUNNING, VMState.PAUSED):
                    continue
                if info.pid is None:
                    stale.append(vm_id)
                    continue
                try:
                    os.kill(info.pid, 0)
                except ProcessLookupError:
                    stale.append(vm_id)
                except PermissionError:
                    # The process exists but belongs to another user, so it is not stale.
                    pass
            for vm_id in stale:
                self._vms[vm_id] = self._vms[vm_id].model_copy(
                    update={"status": VMState.ERROR, "pid": None}
                )
        return stale
