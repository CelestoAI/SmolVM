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

"""Backend runtime adapter interfaces and request models."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TextIO

from smolvm.types import (
    DesktopEndpoint,
    SnapshotArtifacts,
    SnapshotCapturePolicy,
    SnapshotInfo,
    SnapshotType,
    VMInfo,
    VMState,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass(slots=True, frozen=True)
class RuntimeLaunch:
    """Result of starting or restoring a runtime."""

    pid: int
    control_socket_path: Path | None
    status: VMState
    vsock_uds_path: Path | None = None
    display: DesktopEndpoint | None = None


@dataclass(slots=True, frozen=True)
class SnapshotCreateRequest:
    """Runtime-specific snapshot creation inputs."""

    vm_info: VMInfo
    snapshot_id: str
    snapshot_root: Path
    managed_disk_path: Path
    resume_source: bool
    original_status: VMState
    snapshot_type: SnapshotType = SnapshotType.FULL
    capture_policy: SnapshotCapturePolicy = SnapshotCapturePolicy.ALLOW_PAUSE
    timeout_seconds: float = 600.0
    max_bytes_per_second: int | None = None


@dataclass(slots=True, frozen=True)
class SnapshotCreateResult:
    """Runtime-specific snapshot creation outputs."""

    artifacts: SnapshotArtifacts
    source_status: VMState
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    capture_method: Literal["paused", "live"] = "paused"
    operation_manifest_path: Path | None = None


@dataclass(slots=True, frozen=True)
class SnapshotRestoreRequest:
    """Runtime-specific snapshot restore inputs."""

    snapshot: SnapshotInfo
    managed_disk_path: Path
    log_path: Path
    resume_vm: bool
    boot_timeout: float


@dataclass(slots=True)
class RuntimeContext:
    """Shared manager-owned dependencies passed to runtime adapters."""

    data_dir: Path
    socket_dir: Path
    firmware_dir: Path
    """Per-VM firmware state root. Each VM gets a subdirectory
    ``firmware_dir/{vm_id}/`` containing its OVMF NVRAM (``OVMF_VARS.fd``)
    and swtpm state (``swtpm/``). Empty for VMs that don't use UEFI
    firmware boot. Created by the manager during ``create()``."""
    log_files: dict[str, TextIO]
    process_handles: dict[int, subprocess.Popen[bytes]]
    resolve_boot_args: Callable[[VMInfo], str]
    start_firecracker: Callable[[Path, Path], subprocess.Popen[bytes]]
    start_qemu: Callable[..., subprocess.Popen[bytes]]
    unlink_socket: Callable[[Path], None]
    kill_process: Callable[[int], None]
    wait_for_process: Callable[[int, float], None]
    is_process_running: Callable[[int], bool]
    find_qemu_binary: Callable[[], Path | None]
    start_libkrun: Callable[[VMInfo, Path], subprocess.Popen[bytes]] | None = None

    # -- Async callable counterparts (populated when async operations are used) --
    async_start_libkrun: Callable[[VMInfo, Path], Awaitable[asyncio.subprocess.Process]] | None = (
        field(default=None)
    )


class RuntimeAdapter(Protocol):
    """Common runtime control interface used by ``SmolVMManager``."""

    backend: str

    def start(self, vm_info: VMInfo, *, log_path: Path, boot_timeout: float) -> RuntimeLaunch:
        """Boot a VM for the adapter backend."""

    def stop(self, vm_info: VMInfo, *, timeout: float) -> None:
        """Stop a VM process for the adapter backend."""

    def pause(self, vm_info: VMInfo) -> None:
        """Pause a running VM."""

    def resume(self, vm_info: VMInfo) -> None:
        """Resume a paused VM."""

    def create_snapshot(self, request: SnapshotCreateRequest) -> SnapshotCreateResult:
        """Create a snapshot from a VM."""

    def restore_snapshot(self, request: SnapshotRestoreRequest) -> RuntimeLaunch:
        """Restore a snapshot into a runtime."""

    async def async_start(
        self, vm_info: VMInfo, *, log_path: Path, boot_timeout: float
    ) -> RuntimeLaunch:
        """Async version of :meth:`start`."""

    async def async_stop(self, vm_info: VMInfo, *, timeout: float) -> None:
        """Async version of :meth:`stop`."""
