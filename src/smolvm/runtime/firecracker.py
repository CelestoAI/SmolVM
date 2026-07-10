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

"""Firecracker runtime adapter."""

from __future__ import annotations

import asyncio
import logging
import shutil
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smolvm.api import FirecrackerClient
from smolvm.exceptions import SmolVMError
from smolvm.host.disk import clone_or_sparse_copy
from smolvm.runtime.backends import BACKEND_FIRECRACKER
from smolvm.runtime.base import (
    RuntimeAdapter,
    RuntimeContext,
    RuntimeLaunch,
    SnapshotCreateRequest,
    SnapshotCreateResult,
    SnapshotRestoreRequest,
)
from smolvm.types import SnapshotArtifacts, SnapshotType, VMInfo, VMState

logger = logging.getLogger(__name__)


class FirecrackerRuntimeAdapter(RuntimeAdapter):
    """Hypervisor control for the Firecracker backend."""

    backend = BACKEND_FIRECRACKER

    def __init__(self, context: RuntimeContext) -> None:
        self._context = context

    def start(self, vm_info: VMInfo, *, log_path: Path, boot_timeout: float) -> RuntimeLaunch:
        """Start a Firecracker VM and configure it via the API socket."""
        control_socket_path = self._context.socket_dir / f"fc-{vm_info.vm_id}.sock"
        if control_socket_path.exists():
            self._context.unlink_socket(control_socket_path)

        process: Any | None = None
        client: FirecrackerClient | None = None
        try:
            process = self._context.start_firecracker(control_socket_path, log_path)
            client = FirecrackerClient(control_socket_path)
            client.wait_for_socket(timeout=boot_timeout)
            client.set_boot_source(
                vm_info.config.kernel_path, self._context.resolve_boot_args(vm_info)
            )
            client.set_machine_config(vm_info.config.vcpu_count, vm_info.config.memory)
            client.add_drive(
                "rootfs",
                vm_info.config.rootfs_path,
                is_root_device=True,
                is_read_only=False,
            )
            for index, drive_path in enumerate(vm_info.config.extra_drives):
                drive_id = "data_drive" if index == 0 else f"data_drive_{index}"
                client.add_drive(drive_id, drive_path, is_root_device=False, is_read_only=False)
            if vm_info.network is None:
                raise SmolVMError("VM has no network configuration", {"vm_id": vm_info.vm_id})
            client.add_network_interface(
                "eth0",
                vm_info.network.tap_device,
                vm_info.network.guest_mac,
                rate_limit_mbps=vm_info.config.network_rate_limit_mbps,
            )
            # Optional vsock device for host↔guest communication
            vsock_uds_path: str | None = None
            if vm_info.config.vsock:
                vsock_uds_path = vm_info.config.vsock.uds_path or str(
                    self._context.socket_dir / f"vsock-{vm_info.vm_id}.sock"
                )
                # Remove stale socket if present
                vsock_path = Path(vsock_uds_path)
                if vsock_path.exists():
                    vsock_path.unlink()
                client.add_vsock_device(vm_info.config.vsock.guest_cid, vsock_uds_path)
            client.start_instance()
            return RuntimeLaunch(
                pid=process.pid,
                control_socket_path=control_socket_path,
                status=VMState.RUNNING,
                vsock_uds_path=Path(vsock_uds_path) if vsock_uds_path else None,
            )
        except Exception:
            if process is not None:
                with suppress(Exception):
                    self._context.kill_process(process.pid)
            if control_socket_path.exists():
                with suppress(Exception):
                    self._context.unlink_socket(control_socket_path)
            raise
        finally:
            if client is not None:
                with suppress(Exception):
                    client.close()

    async def async_start(
        self, vm_info: VMInfo, *, log_path: Path, boot_timeout: float
    ) -> RuntimeLaunch:
        """Async version of :meth:`start`."""
        return await asyncio.to_thread(
            self.start, vm_info, log_path=log_path, boot_timeout=boot_timeout
        )

    async def async_stop(self, vm_info: VMInfo, *, timeout: float) -> None:
        """Async version of :meth:`stop`."""
        await asyncio.to_thread(self.stop, vm_info, timeout=timeout)

    def stop(self, vm_info: VMInfo, *, timeout: float) -> None:
        """Stop a Firecracker VM."""
        if (
            vm_info.status == VMState.RUNNING
            and vm_info.control_socket_path
            and vm_info.control_socket_path.exists()
        ):
            try:
                client = FirecrackerClient(vm_info.control_socket_path)
                client.send_ctrl_alt_del()
                client.close()
                if vm_info.pid:
                    self._context.wait_for_process(vm_info.pid, timeout)
            except Exception:
                logger.exception(
                    "Failed to gracefully stop Firecracker VM %s via control socket",
                    vm_info.vm_id,
                )

        if vm_info.pid and self._context.is_process_running(vm_info.pid):
            self._context.kill_process(vm_info.pid)

        if vm_info.control_socket_path and vm_info.control_socket_path.exists():
            self._context.unlink_socket(vm_info.control_socket_path)

    def pause(self, vm_info: VMInfo) -> None:
        """Pause a running Firecracker VM."""
        client = self._require_client(vm_info)
        try:
            client.pause_vm()
        finally:
            client.close()

    def resume(self, vm_info: VMInfo) -> None:
        """Resume a paused Firecracker VM."""
        client = self._require_client(vm_info)
        try:
            client.resume_vm()
        finally:
            client.close()

    def create_snapshot(self, request: SnapshotCreateRequest) -> SnapshotCreateResult:
        """Create a Firecracker snapshot and copy the managed disk.

        The Firecracker VM state and guest memory are always captured in full
        so the snapshot restores on its own. ``snapshot_type=DIFF`` only
        changes how the disk is copied: a copy-on-write reflink clone that
        shares unchanged blocks with the source on CoW filesystems, instead of
        a full byte-for-byte copy.
        """
        vm_info = request.vm_info
        client = self._require_client(vm_info)
        try:
            state_path = request.snapshot_root / "vmstate.bin"
            memory_path = request.snapshot_root / "mem.bin"
            disk_path = request.snapshot_root / "disk.ext4"

            if request.original_status == VMState.RUNNING:
                client.pause_vm()
            captured_at = datetime.now(timezone.utc)

            if request.snapshot_type == SnapshotType.DISK:
                shutil.copy2(request.managed_disk_path, disk_path)
                return SnapshotCreateResult(
                    artifacts=SnapshotArtifacts(disk_path=disk_path),
                    source_status=VMState.PAUSED,
                    captured_at=captured_at,
                )

            client.create_snapshot(state_path, memory_path, snapshot_type="Full")
            if request.snapshot_type == SnapshotType.DIFF:
                clone_or_sparse_copy(request.managed_disk_path, disk_path)
            else:
                shutil.copy2(request.managed_disk_path, disk_path)
            return SnapshotCreateResult(
                artifacts=SnapshotArtifacts(
                    state_path=state_path,
                    memory_path=memory_path,
                    disk_path=disk_path,
                ),
                source_status=VMState.PAUSED,
                captured_at=captured_at,
            )
        finally:
            with suppress(Exception):
                client.close()

    def restore_snapshot(self, request: SnapshotRestoreRequest) -> RuntimeLaunch:
        """Restore a Firecracker snapshot into a new runtime process."""
        snapshot = request.snapshot
        state_path = snapshot.artifacts.state_path
        memory_path = snapshot.artifacts.memory_path
        vsock_uds_path = None
        if snapshot.vm_config.vsock is not None:
            vsock_uds_path = (
                Path(snapshot.vm_config.vsock.uds_path)
                if snapshot.vm_config.vsock.uds_path
                else self._context.socket_dir / f"vsock-{snapshot.vm_id}.sock"
            )

        request.managed_disk_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot.artifacts.disk_path, request.managed_disk_path)

        if snapshot.snapshot_type == SnapshotType.DISK:
            effective_config = snapshot.vm_config.model_copy(
                update={"rootfs_path": request.managed_disk_path}
            )
            vm_info = VMInfo(
                vm_id=snapshot.vm_id,
                status=VMState.CREATED,
                config=effective_config,
                network=snapshot.network_config,
            )
            launch = self.start(
                vm_info, log_path=request.log_path, boot_timeout=request.boot_timeout
            )
            if request.resume_vm:
                return launch
            paused_info = VMInfo(
                vm_id=snapshot.vm_id,
                status=launch.status,
                config=effective_config,
                network=snapshot.network_config,
                pid=launch.pid,
                control_socket_path=launch.control_socket_path,
                vsock_uds_path=launch.vsock_uds_path,
            )
            try:
                self.pause(paused_info)
            except Exception:
                with suppress(Exception):
                    self.stop(paused_info, timeout=5.0)
                raise
            return RuntimeLaunch(
                pid=launch.pid,
                control_socket_path=launch.control_socket_path,
                status=VMState.PAUSED,
                vsock_uds_path=launch.vsock_uds_path,
            )

        if state_path is None or memory_path is None:
            raise SmolVMError(
                "Firecracker snapshot restore requires state and memory artifacts",
                {"snapshot_id": snapshot.snapshot_id},
            )

        control_socket_path = self._context.socket_dir / f"fc-{snapshot.vm_id}.sock"
        if control_socket_path.exists():
            self._context.unlink_socket(control_socket_path)
        if vsock_uds_path is not None and vsock_uds_path.exists():
            self._context.unlink_socket(vsock_uds_path)

        process: Any | None = None
        client: FirecrackerClient | None = None
        try:
            process = self._context.start_firecracker(control_socket_path, request.log_path)
            client = FirecrackerClient(control_socket_path)
            client.wait_for_socket(timeout=request.boot_timeout)
            client.load_snapshot(state_path, memory_path, resume_vm=request.resume_vm)
            return RuntimeLaunch(
                pid=process.pid,
                control_socket_path=control_socket_path,
                status=VMState.RUNNING if request.resume_vm else VMState.PAUSED,
                vsock_uds_path=vsock_uds_path,
            )
        except Exception:
            if process is not None:
                with suppress(Exception):
                    self._context.kill_process(process.pid)
            if control_socket_path.exists():
                with suppress(Exception):
                    self._context.unlink_socket(control_socket_path)
            raise
        finally:
            if client is not None:
                with suppress(Exception):
                    client.close()

    @staticmethod
    def _require_client(vm_info: VMInfo) -> FirecrackerClient:
        """Build a Firecracker client for a running or paused VM."""
        if vm_info.control_socket_path is None:
            raise SmolVMError("VM has no Firecracker socket path", {"vm_id": vm_info.vm_id})
        if not vm_info.control_socket_path.exists():
            raise SmolVMError(
                "Firecracker socket is not available",
                {
                    "vm_id": vm_info.vm_id,
                    "socket_path": str(vm_info.control_socket_path),
                },
            )
        return FirecrackerClient(vm_info.control_socket_path)
