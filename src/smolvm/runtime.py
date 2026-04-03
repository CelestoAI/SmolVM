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

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TextIO

from smolvm.types import SnapshotArtifacts, SnapshotInfo, VMInfo, VMState

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(slots=True, frozen=True)
class RuntimeLaunch:
    """Result of starting or restoring a runtime."""

    pid: int
    control_socket_path: Path | None
    status: VMState


@dataclass(slots=True, frozen=True)
class SnapshotCreateRequest:
    """Runtime-specific snapshot creation inputs."""

    vm_info: VMInfo
    snapshot_id: str
    snapshot_root: Path
    managed_disk_path: Path
    resume_source: bool
    original_status: VMState


@dataclass(slots=True, frozen=True)
class SnapshotCreateResult:
    """Runtime-specific snapshot creation outputs."""

    artifacts: SnapshotArtifacts
    source_status: VMState


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
    log_files: dict[str, TextIO]
    resolve_boot_args: Callable[[VMInfo], str]
    start_firecracker: Callable[[Path, Path], subprocess.Popen[bytes]]
    start_qemu: Callable[..., subprocess.Popen[bytes]]
    unlink_socket: Callable[[Path], None]
    kill_process: Callable[[int], None]
    wait_for_process: Callable[[int, float], None]
    is_process_running: Callable[[int], bool]
    find_qemu_binary: Callable[[], Path | None]


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
