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

"""Low-level SmolVM manager.

Orchestrates VM lifecycle, networking, and state management across runtimes.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import platform
import pwd
import re
import shlex
import shutil
import signal
import socket
import subprocess
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from smolvm.comm.select import resolve_comm_channel
from smolvm.exceptions import (
    NetworkError,
    SmolVMError,
    SnapshotAlreadyExistsError,
    SnapshotNotFoundError,
    VMNotFoundError,
)
from smolvm.host.manager import HostCapability, HostManager
from smolvm.host.network import NetworkManager, check_network_prerequisites, resolve_domains_to_ips
from smolvm.runtime.backends import (
    BACKEND_FIRECRACKER,
    BACKEND_LIBKRUN,
    BACKEND_QEMU,
    resolve_backend,
)
from smolvm.runtime.base import RuntimeContext, SnapshotCreateRequest, SnapshotRestoreRequest
from smolvm.runtime.firecracker import FirecrackerRuntimeAdapter
from smolvm.runtime.guest_platforms import get_guest_platform
from smolvm.runtime.libkrun import LibkrunRuntimeAdapter
from smolvm.runtime.qemu import QEMU_ROOT_NODE_NAME, QemuRuntimeAdapter
from smolvm.runtime.qemu_args import build_qemu_argv
from smolvm.storage import (
    SSH_PORT_END,
    SSH_PORT_START,
    StateManagerProtocol,
    create_state_manager,
    ip_to_pool_index,
)
from smolvm.types import (
    GuestOS,
    NetworkConfig,
    SnapshotInfo,
    SnapshotType,
    VMConfig,
    VMInfo,
    VMState,
    VsockConfig,
)
from smolvm.utils import RUNTIME_PRIVILEGE_SETUP_HINT, which

logger = logging.getLogger(__name__)

# Default paths
DEFAULT_DATA_DIR_ENV = "SMOLVM_DATA_DIR"
DEFAULT_SYSTEM_DATA_DIR = Path("/var/lib/smolvm")
DEFAULT_SOCKET_DIR = Path("/tmp")

# Backend-specific defaults
QEMU_GUEST_IP = "10.0.2.15"


def _linux_os_release_ids(os_release_path: Path = Path("/etc/os-release")) -> set[str]:
    """Return Linux distribution IDs from /etc/os-release."""
    ids: set[str] = set()
    try:
        lines = os_release_path.read_text().splitlines()
    except OSError:
        return ids

    for line in lines:
        key, sep, value = line.partition("=")
        if sep != "=" or key not in {"ID", "ID_LIKE"}:
            continue
        value = value.strip().strip('"').strip("'")
        ids.update(part.strip().lower() for part in value.split() if part.strip())
    return ids


def _qemu_system_package_for_host() -> str:
    """Return the distro package that provides the host-arch qemu-system binary."""
    arch = platform.machine().lower()
    if arch in {"arm64", "aarch64"}:
        return "qemu-system-arm"
    return "qemu-system-x86"


def _qemu_install_hint() -> str:
    """Return a host-specific QEMU installation hint."""
    system = platform.system()
    if system == "Darwin":
        return "Install QEMU with 'brew install qemu'."
    if system == "Linux":
        ids = _linux_os_release_ids()
        qemu_system_pkg = _qemu_system_package_for_host()
        if ids & {"debian", "ubuntu"}:
            return (
                "Install QEMU with 'sudo apt-get update && sudo apt-get install -y "
                f"{qemu_system_pkg} qemu-utils'."
            )
        if ids & {"fedora", "rhel", "centos"}:
            return f"Install QEMU with 'sudo dnf install -y {qemu_system_pkg} qemu-img'."
        if ids & {"arch"}:
            return f"Install QEMU with 'sudo pacman -S --needed {qemu_system_pkg} qemu-img'."
        if ids & {"alpine"}:
            qemu_system_bin_pkg = "qemu-system-aarch64"
            if qemu_system_pkg == "qemu-system-x86":
                qemu_system_bin_pkg = "qemu-system-x86_64"
            return f"Install QEMU with 'sudo apk add {qemu_system_bin_pkg} qemu-img'."
        return (
            "Install QEMU with your Linux package manager, then make sure "
            "qemu-system-x86_64 or qemu-system-aarch64 is on PATH."
        )
    return (
        "Install QEMU for this operating system, then make sure qemu-system-x86_64 "
        "or qemu-system-aarch64 is on PATH."
    )


QEMU_GATEWAY_IP = "10.0.2.2"
QEMU_NETMASK = "255.255.255.0"
SNAPSHOT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$|^[a-z0-9]$")


def _get_sudo_user_info() -> pwd.struct_passwd | None:
    """Return sudo user's passwd entry when running under sudo."""
    if os.geteuid() != 0:
        return None

    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user or sudo_user == "root":
        return None

    try:
        return pwd.getpwnam(sudo_user)
    except KeyError:
        return None


def _is_usable_dir(path: Path) -> bool:
    """Return True if *path* exists/is creatable and writable."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    return os.access(path, os.W_OK | os.X_OK)


def _candidate_data_dirs() -> list[Path]:
    """Build candidate data directories from most to least preferred."""
    sudo_user = _get_sudo_user_info()
    if sudo_user is not None:
        user_home = Path(sudo_user.pw_dir)
        xdg_state_home = user_home / ".local" / "state"
    else:
        user_home = Path.home()
        xdg_state_home_env = os.environ.get("XDG_STATE_HOME")
        if xdg_state_home_env:
            xdg_state_home = Path(xdg_state_home_env).expanduser()
        else:
            xdg_state_home = user_home / ".local" / "state"

    user_state_dir = xdg_state_home / "smolvm"

    if os.geteuid() == 0 and sudo_user is None:
        # Direct root session: keep system path first.
        return [DEFAULT_SYSTEM_DATA_DIR, user_state_dir]

    return [user_state_dir, DEFAULT_SYSTEM_DATA_DIR]


def resolve_data_dir(data_dir: Path | None = None) -> Path:
    """Resolve and ensure the data directory path.

    Priority:
    1) Explicit ``data_dir`` argument.
    2) ``SMOLVM_DATA_DIR`` environment override.
    3) Auto-detected writable defaults (user state dir first for dev UX).
    """
    if data_dir is not None:
        resolved = data_dir.expanduser()
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    env_override = os.environ.get(DEFAULT_DATA_DIR_ENV)
    if env_override:
        resolved = Path(env_override).expanduser()
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    for candidate in _candidate_data_dirs():
        candidate = candidate.expanduser()
        if _is_usable_dir(candidate):
            return candidate

    attempted = ", ".join(str(p) for p in _candidate_data_dirs())
    raise PermissionError(
        f"Unable to find a writable SmolVM data directory. Tried: {attempted}. "
        f"Set {DEFAULT_DATA_DIR_ENV} to an explicit writable path."
    )


def _crashed_message(vm_id: str) -> str:
    """User-facing message for a VM whose process is gone."""
    return (
        f"VM '{vm_id}' is not running — its process has exited; "
        f"run 'smolvm delete {vm_id}' to clear it."
    )


class SmolVMManager:
    """Low-level manager class for orchestrating sandbox VMs.

    Provides high-level operations for creating, starting, stopping,
    and managing VMs with proper state persistence and cleanup.
    """

    def __init__(
        self,
        data_dir: Path | None = None,
        socket_dir: Path | None = None,
        backend: str | None = None,
    ) -> None:
        """Initialize the SmolVM manager.

        Args:
            data_dir: Directory for state database. If omitted, SmolVM resolves
                a writable default (``$SMOLVM_DATA_DIR`` -> user state dir ->
                ``/var/lib/smolvm`` as fallback).
            socket_dir: Directory for VM sockets (default: /tmp).
            backend: Runtime backend (``firecracker``, ``qemu``, or ``auto``).
                Defaults to ``auto`` via :func:`smolvm.runtime.backends.resolve_backend`.
        """
        self.data_dir = resolve_data_dir(data_dir)
        self.socket_dir = socket_dir or DEFAULT_SOCKET_DIR
        self.backend = resolve_backend(backend)
        self.disk_dir = self.data_dir / "disks"
        self.snapshot_dir = self.data_dir / "snapshots"
        self.disk_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

        # Under sudo, keep the chosen user-state path owned by the real user.
        owner = _get_sudo_user_info()
        if owner is not None:
            self._ensure_path_owner(self.data_dir, owner.pw_uid, owner.pw_gid)
            self._ensure_path_owner(self.disk_dir, owner.pw_uid, owner.pw_gid)
            self._ensure_path_owner(self.snapshot_dir, owner.pw_uid, owner.pw_gid)

        # Initialize managers
        db_path = self.data_dir / "smolvm.db"
        self.state: StateManagerProtocol = create_state_manager(db_path=db_path)
        if owner is not None:
            self._ensure_path_owner(db_path, owner.pw_uid, owner.pw_gid)
        self.network = NetworkManager()
        self.host = HostManager()

        # Track open log file handles per VM for proper cleanup
        self._log_files: dict[str, TextIO] = {}
        # Retain Popen handles so SIGKILL'd children can be reaped via Popen.wait()
        # — without this, killed VMs linger as zombies and _is_process_running stays True.
        self._process_handles: dict[int, subprocess.Popen[bytes]] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

        logger.info(
            "SmolVM initialized: data_dir=%s, socket_dir=%s, backend=%s",
            self.data_dir,
            self.socket_dir,
            self.backend,
        )

    @staticmethod
    def _ensure_path_owner(path: Path, uid: int, gid: int) -> None:
        """Best-effort ownership alignment for sudo-created paths."""
        try:
            if not path.exists():
                return
            st = path.stat()
            if st.st_uid == uid and st.st_gid == gid:
                return
            os.chown(path, uid, gid)
        except OSError as exc:
            logger.debug("Could not update ownership for %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Lifecycle & class methods
    # ------------------------------------------------------------------

    @classmethod
    def from_id(cls, vm_id: str, **kwargs: Any) -> SmolVMManager:
        """Create an SDK instance and verify a VM exists.

        Useful for attaching to an existing VM from a different process.

        Args:
            vm_id: The VM identifier to look up.
            **kwargs: Forwarded to :meth:`__init__` (e.g. ``data_dir``).

        Returns:
            A :class:`SmolVMManager` instance whose state DB contains *vm_id*.

        Raises:
            VMNotFoundError: If no VM with this ID exists.
        """
        sdk = cls(**kwargs)
        sdk.state.get_vm(vm_id)  # raises VMNotFoundError if absent
        return sdk

    def __enter__(self) -> SmolVMManager:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Release all resources held by this manager.

        Closes open log-file handles.  Calling ``close()`` more than once
        is safe (idempotent).
        """
        if self._closed:
            return
        for fh in self._log_files.values():
            with suppress(Exception):
                fh.close()
        self._log_files.clear()
        for handle in self._process_handles.values():
            with suppress(Exception):
                handle.wait(timeout=0)
        self._process_handles.clear()
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        self._background_tasks.clear()
        self._closed = True
        logger.debug("SmolVM resources released")

    def _resolve_vm_backend(self, backend_override: str | None) -> str:
        """Resolve a backend override with helpful error wrapping."""
        try:
            return resolve_backend(backend_override)
        except ValueError as e:
            raise SmolVMError(str(e)) from e

    def _backend_for_config(self, config: VMConfig) -> str:
        """Return effective backend for the provided VM config."""
        if config.backend:
            return self._resolve_vm_backend(config.backend)
        return self.backend

    def _backend_for_vm(self, vm_info: VMInfo) -> str:
        """Return effective backend for a persisted VM."""
        if vm_info.config.backend:
            return self._resolve_vm_backend(vm_info.config.backend)
        return self.backend

    def _runtime_context(self) -> RuntimeContext:
        """Build a shared adapter context from manager-owned helpers."""
        firmware_dir = self.data_dir / "firmware"
        firmware_dir.mkdir(parents=True, exist_ok=True)
        return RuntimeContext(
            data_dir=self.data_dir,
            socket_dir=self.socket_dir,
            firmware_dir=firmware_dir,
            log_files=self._log_files,
            process_handles=self._process_handles,
            resolve_boot_args=self._resolve_boot_args,
            start_firecracker=self._start_firecracker,
            start_qemu=self._start_qemu,
            start_libkrun=self._start_libkrun,
            unlink_socket=self._unlink_socket,
            kill_process=self._kill_process,
            wait_for_process=self._wait_for_process,
            is_process_running=self._is_process_running,
            find_qemu_binary=self._find_qemu_binary,
            async_start_libkrun=self._async_start_libkrun,
        )

    def _runtime_adapter_for_backend(
        self, backend: str
    ) -> FirecrackerRuntimeAdapter | QemuRuntimeAdapter | LibkrunRuntimeAdapter:
        """Construct a runtime adapter for the requested backend."""
        context = self._runtime_context()
        if backend == BACKEND_FIRECRACKER:
            return FirecrackerRuntimeAdapter(context)
        if backend == BACKEND_QEMU:
            return QemuRuntimeAdapter(context)
        if backend == BACKEND_LIBKRUN:
            return LibkrunRuntimeAdapter(context)
        raise SmolVMError("Unsupported backend", {"backend": backend})

    def _runtime_adapter_for_vm(
        self, vm_info: VMInfo
    ) -> FirecrackerRuntimeAdapter | QemuRuntimeAdapter | LibkrunRuntimeAdapter:
        """Resolve the runtime adapter for a persisted VM."""
        return self._runtime_adapter_for_backend(self._backend_for_vm(vm_info))

    def _runtime_adapter_for_snapshot(
        self,
        snapshot: SnapshotInfo,
    ) -> FirecrackerRuntimeAdapter | QemuRuntimeAdapter | LibkrunRuntimeAdapter:
        """Resolve the runtime adapter for a persisted snapshot."""
        return self._runtime_adapter_for_backend(snapshot.backend)

    def _instance_disk_path(self, vm_id: str, backend: str) -> Path:
        """Return the backend-specific managed isolated disk path for a VM ID."""
        suffix = ".qcow2" if backend == BACKEND_QEMU else ".ext4"
        return self.disk_dir / f"{vm_id}{suffix}"

    @staticmethod
    def _restore_staging_disk_path(managed_disk_path: Path) -> Path:
        """Return a unique temporary disk path used while restoring a snapshot."""
        return managed_disk_path.with_name(f"{managed_disk_path.name}.restore-{uuid4().hex}")

    @staticmethod
    def _restore_backup_disk_path(managed_disk_path: Path) -> Path:
        """Return a unique backup path for an existing managed disk during restore."""
        return managed_disk_path.with_name(f"{managed_disk_path.name}.backup-{uuid4().hex}")

    def _find_qemu_img_binary(self) -> Path | None:
        """Find an available ``qemu-img`` binary."""
        return which("qemu-img")

    def _convert_qemu_managed_disk(self, source_path: Path, target_path: Path) -> None:
        """Create a managed qcow2 disk for the QEMU backend (full copy)."""
        qemu_img = self._find_qemu_img_binary()
        if qemu_img is None:
            raise SmolVMError("QEMU backend requires qemu-img to materialize managed disks")

        source_format = "qcow2" if source_path.suffix == ".qcow2" else "raw"
        result = subprocess.run(
            [
                str(qemu_img),
                "convert",
                "-f",
                source_format,
                "-O",
                "qcow2",
                str(source_path),
                str(target_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SmolVMError(
                "qemu-img convert failed while creating a managed disk",
                {
                    "source_path": str(source_path),
                    "target_path": str(target_path),
                    "stderr": result.stderr.strip(),
                },
            )

    def _create_qemu_overlay_disk(self, base_path: Path, overlay_path: Path) -> None:
        """Create a thin qcow2 overlay backed by a shared base image.

        The overlay file is near-instant to create and consumes negligible disk
        space until the guest writes data. Reads that miss the overlay fall
        through to the read-only base image.
        """
        qemu_img = self._find_qemu_img_binary()
        if qemu_img is None:
            raise SmolVMError("QEMU backend requires qemu-img to create overlay disks")

        base_format = "qcow2" if base_path.suffix == ".qcow2" else "raw"
        result = subprocess.run(
            [
                str(qemu_img),
                "create",
                "-f",
                "qcow2",
                "-b",
                str(base_path.resolve()),
                "-F",
                base_format,
                str(overlay_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SmolVMError(
                "qemu-img create (overlay) failed",
                {
                    "base_path": str(base_path),
                    "overlay_path": str(overlay_path),
                    "stderr": result.stderr.strip(),
                },
            )
        logger.debug(
            "Created thin qcow2 overlay: %s -> base %s",
            overlay_path,
            base_path,
        )

    @staticmethod
    def _copy_with_reflink(source_path: Path, target_path: Path) -> None:
        """Copy a file using reflink (CoW) when the filesystem supports it.

        On btrfs and XFS with reflinks, this is near-instant regardless of
        file size. On other filesystems it falls back to a regular copy.
        """
        result = subprocess.run(
            ["cp", "--reflink=auto", str(source_path), str(target_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # Fallback: --reflink=auto may not be supported on all platforms
            # (e.g. macOS cp doesn't have this flag).
            shutil.copy2(source_path, target_path)

    def _materialize_rootfs(self, config: VMConfig) -> VMConfig:
        """Materialize the effective rootfs path for a VM create request.

        In ``isolated`` mode, creates a per-VM disk under ``data_dir/disks``
        (or reuses an existing one for the same VM ID).

        **QEMU:** Creates a thin qcow2 overlay backed by the shared base image.
        Near-instant and consumes minimal disk — reads fall through to the
        shared base, writes go to the per-VM overlay.

        **Firecracker:** Copies the base image using ``cp --reflink=auto``.
        On btrfs/XFS this is instant copy-on-write; on other filesystems it
        falls back to a regular copy.

        In ``shared`` mode, uses ``rootfs_path`` directly.
        """
        if config.disk_mode == "shared":
            return config

        backend = self._backend_for_config(config)
        instance_rootfs = self._instance_disk_path(config.vm_id, backend)
        if not instance_rootfs.exists():
            logger.info(
                "Creating isolated disk for VM %s from %s -> %s",
                config.vm_id,
                config.rootfs_path,
                instance_rootfs,
            )
            if backend == BACKEND_QEMU:
                self._create_qemu_overlay_disk(config.rootfs_path, instance_rootfs)
            else:
                self._copy_with_reflink(config.rootfs_path, instance_rootfs)
        else:
            logger.info(
                "Reusing isolated disk for VM %s at %s",
                config.vm_id,
                instance_rootfs,
            )

        return config.model_copy(update={"rootfs_path": instance_rootfs})

    def _materialize_firmware(self, config: VMConfig) -> None:
        """Create the per-VM OVMF NVRAM file for UEFI-firmware guests.

        For Windows guests (the only Phase 1 firmware-required platform),
        copies the system-wide ``OVMF_VARS.fd`` template to
        ``data_dir/firmware/{vm_id}/OVMF_VARS.fd``. Subsequent VM starts
        read and write the per-VM file directly — Secure Boot enrollment
        and UEFI boot order persist across reboots.

        No-op for Linux guests (no UEFI NVRAM needed). Idempotent: if
        the per-VM file already exists, it is preserved as-is so prior
        Secure Boot state isn't clobbered.
        """
        if config.guest_os is not GuestOS.WINDOWS:
            return

        # Resolving the spec also surfaces the macOS-host rejection and
        # the missing-OVMF install hint at create time, rather than at
        # start time when the user has already paid the cost of
        # materializing the rootfs.
        try:
            spec = get_guest_platform(
                config.guest_os,
                host_system=platform.system(),
                arch=platform.machine(),
            )
        except (NotImplementedError, ValueError) as exc:
            raise SmolVMError(str(exc), {"vm_id": config.vm_id}) from exc

        if spec.firmware is None:
            return  # belt-and-braces; Windows spec always sets this

        vm_firmware_dir = self.data_dir / "firmware" / config.vm_id
        vm_firmware_dir.mkdir(parents=True, exist_ok=True)
        target = vm_firmware_dir / "OVMF_VARS.fd"

        if target.exists():
            logger.info(
                "Reusing per-VM OVMF NVRAM for VM %s at %s",
                config.vm_id,
                target,
            )
            return

        template = spec.firmware.vars_template_path
        logger.info(
            "Materializing per-VM OVMF NVRAM for VM %s: %s -> %s",
            config.vm_id,
            template,
            target,
        )
        shutil.copy2(template, target)
        # NVRAM holds the UEFI variable store including any Secure Boot
        # enrollment changes the guest makes. Lock down by-default to
        # the SmolVM data-dir owner only.
        target.chmod(0o600)

    def _managed_disk_for_vm(self, vm_info: VMInfo | None) -> Path | None:
        """Return the managed isolated disk path for a VM if applicable."""
        if vm_info is None:
            return None
        if vm_info.config.disk_mode != "isolated":
            return None

        expected = self._instance_disk_path(vm_info.vm_id, self._backend_for_vm(vm_info)).resolve()
        actual = vm_info.config.rootfs_path.resolve()
        if actual != expected:
            return None
        return expected

    def _check_workspace_mounts(self, vm_info: VMInfo) -> None:
        """Verify each workspace mount's host folder is still usable.

        The Pydantic validator on ``WorkspaceMount.host_path`` runs at
        create time, but storage reads pass ``validate_paths=False`` so a
        config can reach start time after its host folder was deleted or
        replaced. Catch both cases here (missing path, or path that's no
        longer a directory) and raise a single friendly error, instead of
        letting QEMU fail later with a host-side message a first-time user
        cannot interpret.
        """
        bad_paths = [
            mount.host_path
            for mount in vm_info.config.workspace_mounts
            if not (mount.host_path.exists() and mount.host_path.is_dir())
        ]
        if not bad_paths:
            return
        paths = ", ".join(str(p) for p in bad_paths)
        raise SmolVMError(
            f"Cannot start sandbox '{vm_info.vm_id}': shared folder is missing: "
            f"{paths}. Restore it, or run 'smolvm delete {vm_info.vm_id}'.",
            {
                "vm_id": vm_info.vm_id,
                "missing_mounts": [str(p) for p in bad_paths],
            },
        )

    def _ensure_snapshot_supported(self, vm_info: VMInfo) -> None:
        """Validate whether snapshot operations are supported for a VM."""
        if vm_info.config.guest_os is GuestOS.WINDOWS:
            # Snapshotting a Windows VM faithfully needs the qcow2, the OVMF
            # NVRAM, AND the swtpm state captured atomically; that's a
            # standalone design problem (multi-artifact snapshot atomicity)
            # and not in Phase 1 scope.
            raise SmolVMError(
                "Snapshot and restore are not supported for Windows guests in this release.",
                {"vm_id": vm_info.vm_id},
            )
        if vm_info.config.disk_mode != "isolated":
            raise SmolVMError("Snapshotting currently supports only isolated-disk VMs")
        if vm_info.config.extra_drives:
            raise SmolVMError("Snapshotting currently supports only VMs without extra drives")
        if vm_info.config.workspace_mounts:
            raise SmolVMError("Snapshotting is not supported for VMs with workspace mounts")
        if vm_info.network is None:
            raise SmolVMError("VM has no network configuration", {"vm_id": vm_info.vm_id})

    def _warn_low_disk_space_for_snapshot(
        self, vm_info: VMInfo, snapshot_type: SnapshotType = SnapshotType.FULL
    ) -> None:
        """Warn when snapshot creation looks likely to exhaust local disk space."""
        rootfs_size = vm_info.config.rootfs_path.stat().st_size
        # DISK snapshots don't dump guest RAM, so the memory term doesn't apply.
        if snapshot_type == SnapshotType.DISK:
            required_bytes = rootfs_size
        else:
            mem_size = vm_info.config.memory * 1024 * 1024
            required_bytes = rootfs_size + (2 * mem_size)
        free_bytes = shutil.disk_usage(self.snapshot_dir).free
        if free_bytes < required_bytes:
            logger.warning(
                "Low disk space for snapshotting VM %s: free=%d required~=%d",
                vm_info.vm_id,
                free_bytes,
                required_bytes,
            )

    def _snapshot_root_for_id(self, snapshot_id: str) -> Path:
        """Return a validated snapshot directory path under ``self.snapshot_dir``."""
        if not snapshot_id:
            raise ValueError("snapshot_id cannot be empty")
        if not SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
            raise ValueError(
                "snapshot_id must contain only lowercase letters, numbers, hyphens, or underscores"
            )

        snapshot_root = (self.snapshot_dir / snapshot_id).resolve(strict=False)
        snapshot_dir = self.snapshot_dir.resolve()
        try:
            snapshot_root.relative_to(snapshot_dir)
        except ValueError as exc:
            raise ValueError("snapshot_id must resolve within the snapshot directory") from exc
        return snapshot_root

    def _ensure_firecracker_network_for_restore(
        self,
        vm_id: str,
        network: NetworkConfig,
        vm_config: VMConfig | None = None,
    ) -> None:
        """Ensure host-side network resources exist for a restored Firecracker VM."""
        user = os.environ.get("USER", "root")
        self.network.create_tap(network.tap_device, user)
        self.network.configure_tap(
            network.tap_device,
            host_ip=network.gateway_ip,
            netmask="32",
        )
        self.network.add_route(network.guest_ip, network.tap_device)
        self.network.setup_nat(network.tap_device)

        # Re-apply domain allowlist if the original config had one
        if (
            vm_config is not None
            and vm_config.internet_settings is not None
            and not vm_config.internet_settings.is_allow_all_domains
        ):
            allowed_ips = resolve_domains_to_ips(vm_config.internet_settings.allowed_domains)
            self.network.apply_egress_allowlist(network.tap_device, allowed_ips)

        if network.ssh_host_port is not None:
            self.network.setup_ssh_port_forward(
                vm_id=vm_id,
                guest_ip=network.guest_ip,
                host_port=network.ssh_host_port,
            )

    def _teardown_firecracker_network_for_restore(self, vm_id: str, network: NetworkConfig) -> None:
        """Best-effort teardown for host networking provisioned during restore."""
        if network.ssh_host_port is not None:
            with suppress(Exception):
                self.network.cleanup_ssh_port_forward(
                    vm_id=vm_id,
                    guest_ip=network.guest_ip,
                    host_port=network.ssh_host_port,
                )
        with suppress(Exception):
            self.network.cleanup_all_local_port_forwards(vm_id)
        with suppress(Exception):
            self.network.remove_egress_rules(network.tap_device)
        with suppress(Exception):
            self.network.cleanup_nat_rules(network.tap_device)
        with suppress(Exception):
            self.network.cleanup_tap(network.tap_device)

    def _qemu_binary_candidates(self) -> list[str]:
        """Return architecture-aware qemu-system binary candidates."""
        arch = platform.machine().lower()
        if arch in {"arm64", "aarch64"}:
            return ["qemu-system-aarch64", "qemu-system-x86_64"]
        if arch in {"x86_64", "amd64"}:
            return ["qemu-system-x86_64", "qemu-system-aarch64"]
        return ["qemu-system-aarch64", "qemu-system-x86_64"]

    def _find_qemu_binary(self) -> Path | None:
        """Find an available qemu-system binary."""
        for candidate in self._qemu_binary_candidates():
            path = which(candidate)
            if path is not None:
                return path
        return None

    def _qemu_version(self, qemu_bin: Path) -> tuple[int, int, int] | None:
        """Return the parsed qemu-system version."""
        try:
            result = subprocess.run(
                [str(qemu_bin), "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except Exception:
            logger.debug("Could not probe qemu version", exc_info=True)
            return None

        match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", f"{result.stdout}\n{result.stderr}")
        if match is None:
            return None
        major, minor, micro = match.groups()
        return int(major), int(minor), int(micro or 0)

    def _check_qemu_prerequisites(self) -> list[str]:
        """Check host prerequisites for the qemu backend."""
        errors: list[str] = []

        qemu_bin = self._find_qemu_binary()
        if qemu_bin is None:
            errors.append(f"QEMU not found. {_qemu_install_hint()}")
        elif platform.system() == "Darwin":
            try:
                result = subprocess.run(
                    [str(qemu_bin), "-accel", "help"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )
                accel_text = f"{result.stdout}\n{result.stderr}".lower()
                if "hvf" not in accel_text:
                    errors.append(
                        "QEMU binary does not report Hypervisor.framework (hvf) acceleration. "
                        "Install/upgrade Homebrew qemu."
                    )
            except Exception:
                logger.debug("Could not probe qemu accelerators", exc_info=True)

        if qemu_bin is not None:
            version = self._qemu_version(qemu_bin)
            if version is None:
                errors.append("Could not determine QEMU version.")
            elif version < (6, 0, 0):
                errors.append(
                    f"QEMU {version[0]}.{version[1]}.{version[2]} is too old. "
                    "Install QEMU 6.0 or newer."
                )

        if self._find_qemu_img_binary() is None:
            errors.append("'qemu-img' command not found (install qemu)")

        if which("ssh") is None:
            errors.append("'ssh' command not found (install openssh-client)")

        return errors

    def _check_libkrun_prerequisites(self) -> list[str]:
        """Check host prerequisites for the libkrun backend."""
        errors: list[str] = []

        if self._find_krunvm_binary() is None:
            errors.append("'krunvm' command not found (install libkrun/krunvm)")

        if which("ssh") is None:
            errors.append("'ssh' command not found (install openssh-client)")

        return errors

    def check_prerequisites(self) -> list[str]:
        """Check if all prerequisites are met.

        Uses backend-specific validation:
        - firecracker: KVM + Linux networking + Firecracker binary
        - qemu: qemu-system binary + SSH client

        Returns:
            List of error messages (empty if all good).
        """
        if self.backend == BACKEND_QEMU:
            return self._check_qemu_prerequisites()
        if self.backend == BACKEND_LIBKRUN:
            return self._check_libkrun_prerequisites()

        errors = []

        host_info = self.host.validate()

        # KVM check
        if not host_info.capabilities.get(HostCapability.KVM, False):
            errors.append("/dev/kvm not available. Try: sudo usermod -aG kvm $USER")

        # Firecracker check
        if not host_info.capabilities.get(HostCapability.FIRECRACKER, False):
            errors.append("'firecracker' binary not found in PATH or ~/.smolvm/bin/")

        # Dependency checks
        errors.extend(host_info.missing_deps)

        # Network prerequisites
        errors.extend(check_network_prerequisites())

        return errors

    def _maybe_enable_vsock(self, config: VMConfig, backend: str, vm_info: VMInfo) -> VMInfo:
        """Reserve a vsock CID and persist ``config.vsock`` when the VM should
        use the vsock control channel.

        No-op unless the resolved channel is vsock (QEMU on a Linux host with
        ``/dev/vhost-vsock``). The CID is reserved after the VM row exists (the
        ``vsock_cids`` foreign key requires it) and written back to the config
        so ``build_qemu_argv`` wires the ``vhost-vsock`` device at boot.
        Returns the possibly-updated :class:`VMInfo`.
        """
        resolution = resolve_comm_channel(
            requested=None,
            config_channel=config.comm_channel,
            backend=backend,
            guest_os=config.guest_os,
        )
        if resolution.kind != "vsock":
            return vm_info
        cid = self.state.reserve_vsock_cid(config.vm_id)
        updated = config.model_copy(update={"vsock": VsockConfig(guest_cid=cid)})
        logger.info("VM %s will use vsock control channel (CID %d)", config.vm_id, cid)
        return self.state.update_vm(config.vm_id, config=updated)

    @staticmethod
    def _uses_host_tap_networking(config: VMConfig, backend: str) -> bool:
        """Whether a VM is wired to a host TAP device (vs userspace slirp).

        Firecracker always uses a host TAP. QEMU uses one only when the
        config opts in via ``qemu_network='tap'`` — the default stays slirp
        so the macOS/dev path is unchanged. libkrun never uses a host TAP.
        TAP-mode VMs get a real routable IP and fall under the shared
        nftables NAT/isolation rules (egress masquerade, cross-sandbox drop,
        IMDS block); slirp VMs rely on QEMU userspace NAT + host port
        forwards instead.
        """
        if backend == BACKEND_FIRECRACKER:
            return True
        if backend == BACKEND_QEMU:
            return config.qemu_network == "tap"
        return False

    @staticmethod
    def _local_tcp_port_is_available(host: str, port: int) -> bool:
        """Return whether a local TCP forward target can be bound."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((host, port))
        except (OSError, OverflowError):
            return False
        return True

    @classmethod
    def _local_ssh_port_is_available(cls, port: int) -> bool:
        """Return whether the localhost SSH forward port can be bound."""
        return cls._local_tcp_port_is_available("127.0.0.1", port)

    def _reserve_available_ssh_port(self, vm_id: str) -> int:
        """Reserve an SSH host port that is free in state and on localhost."""
        excluded_ports: set[int] = set()
        pool_size = SSH_PORT_END - SSH_PORT_START + 1

        while len(excluded_ports) < pool_size:
            port = self.state.reserve_ssh_port(
                vm_id,
                excluded_host_ports=excluded_ports,
            )
            if self._local_ssh_port_is_available(port):
                return port

            self.state.release_ssh_port(vm_id)
            excluded_ports.add(port)
            logger.warning(
                "Skipping SSH host port %d for VM %s because it is already in use",
                port,
                vm_id,
            )

        raise NetworkError(
            f"No local SSH port is available for sandbox '{vm_id}'. "
            f"Free a port in {SSH_PORT_START}-{SSH_PORT_END}, then retry."
        )

    def _check_qemu_slirp_host_forward_ports(self, vm_info: VMInfo) -> None:
        """Fail before QEMU starts if any slirp host-forward port is busy."""
        if vm_info.config.qemu_network == "tap" or vm_info.network is None:
            return

        # This is an early, user-friendly guard. QEMU still owns the final bind,
        # so a process that grabs the port after this check can still win the race.
        ssh_port = vm_info.network.ssh_host_port
        if ssh_port is not None and not self._local_ssh_port_is_available(ssh_port):
            raise SmolVMError(
                f"Local SSH port {ssh_port} is already in use. Free that port, or run "
                f"'smolvm delete {vm_info.vm_id}' to remove the sandbox.",
                {"vm_id": vm_info.vm_id, "ssh_host_port": ssh_port},
            )

        for forward in vm_info.config.port_forwards:
            if self._local_tcp_port_is_available(forward.host_address, forward.host_port):
                continue
            raise SmolVMError(
                f"Local port {forward.host_address}:{forward.host_port} is already in use. "
                f"Free that port, or run 'smolvm delete {vm_info.vm_id}' to remove the sandbox.",
                {
                    "vm_id": vm_info.vm_id,
                    "host_address": forward.host_address,
                    "host_port": forward.host_port,
                    "guest_port": forward.guest_port,
                },
            )

    def create(self, config: VMConfig) -> VMInfo:
        """Create a new microVM.

        This allocates resources (IP, TAP device) and persists state,
        but does not start the VM.

        Args:
            config: VM configuration.

        Returns:
            VMInfo for the created VM.

        Raises:
            VMAlreadyExistsError: If VM ID already exists.
            NetworkError: If network setup fails.
            ValidationError: If config is invalid.
        """
        if config is None:
            raise ValueError("config cannot be None")

        backend = self._backend_for_config(config)
        effective_config = config
        if effective_config.backend != backend:
            effective_config = effective_config.model_copy(update={"backend": backend})

        if effective_config.workspace_mounts and backend != BACKEND_QEMU:
            raise SmolVMError(
                "Workspace mounts (virtio-9p) are only supported with the "
                f"QEMU backend (got backend={backend!r}). Re-run without "
                "--backend (SmolVM will auto-select QEMU when --mount is "
                "used) or pass --backend qemu explicitly.",
                {"vm_id": effective_config.vm_id, "backend": backend},
            )

        effective_config = self._materialize_rootfs(effective_config)
        self._materialize_firmware(effective_config)

        logger.info(
            "Creating VM: %s (backend=%s, disk_mode=%s)",
            effective_config.vm_id,
            backend,
            effective_config.disk_mode,
        )

        # Create VM record
        vm_info = self.state.create_vm(effective_config)

        try:
            ssh_host_port = self._reserve_available_ssh_port(effective_config.vm_id)

            if not self._uses_host_tap_networking(effective_config, backend):
                if (
                    effective_config.internet_settings is not None
                    and not effective_config.internet_settings.is_allow_all_domains
                ):
                    logger.warning(
                        "internet_settings domain allowlist is not supported "
                        "with the %s backend (user-mode networking)",
                        backend,
                    )
                mac_seed = (ssh_host_port % 65534) + 1
                guest_mac = self.network.generate_mac(mac_seed)
                network_config = NetworkConfig(
                    guest_ip=QEMU_GUEST_IP,
                    gateway_ip=QEMU_GATEWAY_IP,
                    netmask=QEMU_NETMASK,
                    tap_device="usernet",
                    guest_mac=guest_mac,
                    ssh_host_port=ssh_host_port,
                )
                vm_info = self.state.update_vm(effective_config.vm_id, network=network_config)
                vm_info = self._maybe_enable_vsock(effective_config, backend, vm_info)
                logger.info(
                    "VM created: %s (backend=%s, ssh localhost:%d)",
                    effective_config.vm_id,
                    backend,
                    ssh_host_port,
                )
                return vm_info

            # Firecracker networking: allocate an IP first, then derive
            # a unique TAP name from its pool index within 172.16.0.0/16.
            guest_ip = self.state.allocate_ip(effective_config.vm_id, "pending")
            vm_number = ip_to_pool_index(guest_ip)
            tap_name = f"tap{vm_number}"

            # Update the lease with the real TAP name
            self.state.update_ip_lease_tap(effective_config.vm_id, tap_name)

            # Create and configure TAP device
            user = os.environ.get("USER", "root")
            self.network.create_tap(tap_name, user)

            # Use /32 mask to avoid subnet conflicts between multiple TAPs
            self.network.configure_tap(tap_name, netmask="32")
            self.network.add_route(guest_ip, tap_name)

            self.network.setup_nat(tap_name)

            # Apply domain allowlist if configured
            if (
                effective_config.internet_settings is not None
                and not effective_config.internet_settings.is_allow_all_domains
            ):
                allowed_ips = resolve_domains_to_ips(
                    effective_config.internet_settings.allowed_domains
                )
                self.network.apply_egress_allowlist(tap_name, allowed_ips)

            self.network.setup_ssh_port_forward(
                vm_id=effective_config.vm_id,
                guest_ip=guest_ip,
                host_port=ssh_host_port,
            )

            # Generate MAC address from pool index
            guest_mac = self.network.generate_mac(vm_number)

            # Create network config
            network_config = NetworkConfig(
                guest_ip=guest_ip,
                gateway_ip=self.network.host_ip,
                tap_device=tap_name,
                guest_mac=guest_mac,
                ssh_host_port=ssh_host_port,
            )

            # Update VM with network info
            vm_info = self.state.update_vm(effective_config.vm_id, network=network_config)

            # QEMU-on-TAP still uses the vsock control channel (unique per-host
            # CID reserved from state). Firecracker manages its own vsock via
            # the VsockConfig passed directly in the VM config, so this is a
            # no-op there.
            if backend == BACKEND_QEMU:
                vm_info = self._maybe_enable_vsock(effective_config, backend, vm_info)

            logger.info(
                "VM created: %s (IP: %s, TAP: %s)",
                effective_config.vm_id,
                guest_ip,
                tap_name,
            )
            return vm_info

        except Exception as e:
            # Rollback on failure
            logger.error("Failed to create VM %s: %s", effective_config.vm_id, e)
            self._cleanup_resources(effective_config.vm_id)
            # Delete the VM record that was created
            with suppress(Exception):
                self.state.delete_vm(effective_config.vm_id)
            raise

    def start(
        self,
        vm_id: str,
        boot_timeout: float = 30.0,
    ) -> VMInfo:
        """Start a microVM.

        Args:
            vm_id: The VM identifier.
            boot_timeout: Maximum seconds to wait for boot.

        Returns:
            Updated VMInfo.

        Raises:
            VMNotFoundError: If VM doesn't exist.
            SmolVMError: If VM is not in CREATED or STOPPED state.
            TimeoutError: If boot times out.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        logger.info("Starting VM: %s", vm_id)

        # Get current state
        vm_info = self.state.get_vm(vm_id)

        if vm_info.status not in (VMState.CREATED, VMState.STOPPED):
            raise SmolVMError(
                f"Cannot start VM in state '{vm_info.status.value}'",
                {"vm_id": vm_id, "current_status": vm_info.status.value},
            )

        if vm_info.network is None:
            raise SmolVMError(
                "VM has no network configuration",
                {"vm_id": vm_id},
            )

        self._check_workspace_mounts(vm_info)

        backend = self._backend_for_vm(vm_info)
        if backend == BACKEND_QEMU:
            self._check_qemu_slirp_host_forward_ports(vm_info)
        log_path = self.data_dir / f"{vm_id}.log"
        adapter = self._runtime_adapter_for_backend(backend)
        try:
            launch = adapter.start(vm_info, log_path=log_path, boot_timeout=boot_timeout)
            vm_info = self.state.update_vm(
                vm_id,
                status=launch.status,
                pid=launch.pid,
                control_socket_path=launch.control_socket_path,
            )
            logger.info(
                "VM started: %s (backend=%s, PID: %d)",
                vm_id,
                backend,
                launch.pid,
            )
            return vm_info
        except Exception as e:
            logger.error("Failed to start VM %s: %s", vm_id, e)
            self.state.update_vm(
                vm_id,
                status=VMState.ERROR,
                clear_pid=True,
                clear_socket_path=True,
            )
            self._close_runtime_log(vm_id, backend, vm_info.control_socket_path)
            raise

    def stop(self, vm_id: str, timeout: float = 10.0) -> VMInfo:
        """Stop a running microVM.

        Args:
            vm_id: The VM identifier.
            timeout: Seconds to wait for graceful shutdown before killing.

        Returns:
            Updated VMInfo.

        Raises:
            VMNotFoundError: If VM doesn't exist.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        logger.info("Stopping VM: %s", vm_id)

        vm_info = self.state.get_vm(vm_id)

        if vm_info.status not in (VMState.RUNNING, VMState.PAUSED):
            logger.warning("VM %s is not running (status: %s)", vm_id, vm_info.status)
            return vm_info

        backend = self._backend_for_vm(vm_info)
        self._runtime_adapter_for_backend(backend).stop(vm_info, timeout=timeout)
        self._close_runtime_log(vm_id, backend, vm_info.control_socket_path)
        vm_info = self.state.update_vm(
            vm_id,
            status=VMState.STOPPED,
            clear_pid=True,
            clear_socket_path=True,
        )
        logger.info("VM stopped: %s (backend=%s)", vm_id, backend)
        return vm_info

    def pause(self, vm_id: str) -> VMInfo:
        """Pause a running VM."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        vm_info = self.state.get_vm(vm_id)

        if vm_info.status != VMState.RUNNING:
            self._raise_crashed_or_state_error(vm_info, action="pause")

        try:
            self._runtime_adapter_for_vm(vm_info).pause(vm_info)
        except Exception:
            self._raise_if_crashed(vm_info)
            raise
        return self.state.update_vm(vm_id, status=VMState.PAUSED)

    def resume(self, vm_id: str) -> VMInfo:
        """Resume a paused VM."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        vm_info = self.state.get_vm(vm_id)

        if vm_info.status != VMState.PAUSED:
            self._raise_crashed_or_state_error(vm_info, action="resume")

        try:
            self._runtime_adapter_for_vm(vm_info).resume(vm_info)
        except Exception:
            self._raise_if_crashed(vm_info)
            raise
        return self.state.update_vm(vm_id, status=VMState.RUNNING)

    def _raise_crashed_or_state_error(self, vm_info: VMInfo, *, action: str) -> None:
        """Raise the state error, replacing it with a 'crashed' message if stale.

        A VM whose DB row says RUNNING/PAUSED but whose process is gone shows
        up here as a state-guard failure (e.g. resume rejecting a "running"
        VM). Translate that into an actionable crash message instead of the
        misleading "Cannot resume VM in state 'running'".
        """
        refreshed = self.refresh_status(vm_info)
        if refreshed.status == VMState.ERROR and vm_info.status in (
            VMState.RUNNING,
            VMState.PAUSED,
        ):
            raise SmolVMError(
                _crashed_message(vm_info.vm_id),
                {"vm_id": vm_info.vm_id, "current_status": refreshed.status.value},
            )
        # State-appropriate recovery: STOPPED/CREATED → start, ERROR → delete.
        # PAUSED/RUNNING (already in a compatible state) needs no recovery —
        # the status string itself is the explanation.
        recovery = ""
        if vm_info.status == VMState.ERROR:
            recovery = f"; run 'smolvm delete {vm_info.vm_id}' to clear it"
        elif vm_info.status in (VMState.STOPPED, VMState.CREATED):
            recovery = f"; run 'smolvm start {vm_info.vm_id}' to start it"
        raise SmolVMError(
            f"Cannot {action} VM in state '{vm_info.status.value}'{recovery}.",
            {"vm_id": vm_info.vm_id, "current_status": vm_info.status.value},
        )

    def _raise_if_crashed(self, vm_info: VMInfo) -> None:
        """Re-raise as a crash error if the VM's process is gone, otherwise no-op."""
        refreshed = self.refresh_status(vm_info)
        if refreshed.status == VMState.ERROR:
            raise SmolVMError(
                _crashed_message(vm_info.vm_id),
                {"vm_id": vm_info.vm_id, "current_status": refreshed.status.value},
            ) from None

    def create_snapshot(
        self,
        vm_id: str,
        snapshot_id: str | None = None,
        *,
        snapshot_type: SnapshotType = SnapshotType.FULL,
        resume_source: bool = False,
    ) -> SnapshotInfo:
        """Create a snapshot for a paused or running VM.

        ``snapshot_type`` controls how the disk is stored. ``FULL`` (the
        default) writes a self-contained copy that always restores on its own.
        ``DIFF`` stores only what changed since the shared base image, which is
        much smaller but depends on that base image still being present.
        ``DISK`` is self-contained like ``FULL`` but stores only the disk (no
        guest RAM), so it is much faster and lighter; restoring it boots the
        guest fresh from the disk instead of resuming the running state.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        vm_info = self.state.get_vm(vm_id)
        self._ensure_snapshot_supported(vm_info)
        if vm_info.status not in (VMState.RUNNING, VMState.PAUSED):
            raise SmolVMError(
                f"Cannot snapshot VM in state '{vm_info.status.value}'",
                {"vm_id": vm_id, "current_status": vm_info.status.value},
            )

        snapshot_id = snapshot_id or f"snap-{vm_id}-{int(time.time())}"
        snapshot_root = self._snapshot_root_for_id(snapshot_id)
        if snapshot_root.exists():
            raise SnapshotAlreadyExistsError(snapshot_id)
        with suppress(SnapshotNotFoundError):
            self.state.get_snapshot(snapshot_id)
            raise SnapshotAlreadyExistsError(snapshot_id)

        original_status = vm_info.status
        backend = self._backend_for_vm(vm_info)
        managed_disk_path = self._managed_disk_for_vm(vm_info)
        if managed_disk_path is None:
            raise SmolVMError("Snapshotting requires a managed isolated disk", {"vm_id": vm_id})

        adapter = self._runtime_adapter_for_backend(backend)
        snapshot_persisted = False

        try:
            snapshot_root.mkdir(parents=True, exist_ok=False)
            self._warn_low_disk_space_for_snapshot(vm_info, snapshot_type)
            result = adapter.create_snapshot(
                SnapshotCreateRequest(
                    vm_info=vm_info,
                    snapshot_id=snapshot_id,
                    snapshot_root=snapshot_root,
                    managed_disk_path=managed_disk_path,
                    resume_source=resume_source,
                    original_status=original_status,
                    snapshot_type=snapshot_type,
                )
            )

            snapshot_info = SnapshotInfo(
                snapshot_id=snapshot_id,
                vm_id=vm_info.vm_id,
                backend=backend,
                artifacts=result.artifacts,
                vm_config=vm_info.config,
                network_config=vm_info.network,
                created_at=datetime.now(timezone.utc),
                snapshot_type=snapshot_type,
            )
            self.state.create_snapshot(snapshot_info)
            snapshot_persisted = True
            source_status = VMState.PAUSED
            if original_status == VMState.RUNNING and resume_source:
                adapter.resume(vm_info)
                source_status = VMState.RUNNING
            self.state.update_vm(vm_id, status=source_status)
            return snapshot_info
        except Exception as original_error:
            snapshot_dir_removed = False
            rollback_error: Exception | None = None
            try:
                shutil.rmtree(snapshot_root)
                snapshot_dir_removed = True
            except FileNotFoundError:
                snapshot_dir_removed = True
            except Exception as cleanup_error:
                logger.warning(
                    "Failed to remove snapshot directory during rollback for %s: %s",
                    snapshot_id,
                    cleanup_error,
                )
                rollback_error = cleanup_error
            if snapshot_persisted and snapshot_dir_removed:
                with suppress(Exception):
                    self.state.delete_snapshot(snapshot_id)
            if original_status == VMState.RUNNING:
                with suppress(Exception):
                    adapter.resume(vm_info)
                    self.state.update_vm(vm_id, status=VMState.RUNNING)
            if rollback_error is not None:
                raise rollback_error from original_error
            raise

    def restore_snapshot(
        self,
        snapshot_id: str,
        *,
        resume_vm: bool = False,
        force: bool = False,
    ) -> VMInfo:
        """Restore a snapshot back into its original VM identity."""
        if not snapshot_id:
            raise ValueError("snapshot_id cannot be empty")

        snapshot = self.state.get_snapshot(snapshot_id)
        if snapshot.restored and not force:
            raise SmolVMError("Snapshot already restored", {"snapshot_id": snapshot_id})

        restore_vm_id = snapshot.vm_id
        existing_vm: VMInfo | None = None
        with suppress(VMNotFoundError):
            existing_vm = self.state.get_vm(restore_vm_id)

        if snapshot.restored and snapshot.restored_vm_id:
            with suppress(VMNotFoundError):
                restored_vm = self.state.get_vm(snapshot.restored_vm_id)
                if restored_vm.status in (VMState.RUNNING, VMState.PAUSED):
                    raise SmolVMError(
                        "Snapshot is already backing an active restored VM",
                        {
                            "snapshot_id": snapshot_id,
                            "restored_vm_id": snapshot.restored_vm_id,
                        },
                    )

        if existing_vm is not None and existing_vm.config != snapshot.vm_config:
            raise SmolVMError(
                "Snapshot restore requires the original VM identity and config",
                {"snapshot_id": snapshot_id, "vm_id": restore_vm_id},
            )

        if existing_vm is not None and existing_vm.status in (VMState.RUNNING, VMState.PAUSED):
            self.stop(restore_vm_id)
            existing_vm = self.state.get_vm(restore_vm_id)

        if (
            snapshot.vm_config.kernel_path is not None
            and not snapshot.vm_config.kernel_path.exists()
        ):
            raise SmolVMError(
                "Snapshot restore requires the original kernel path to exist",
                {
                    "snapshot_id": snapshot_id,
                    "kernel_path": str(snapshot.vm_config.kernel_path),
                },
            )
        required_artifacts: list[tuple[Path | None, str]] = [
            (snapshot.artifacts.disk_path, "disk_path"),
            (snapshot.artifacts.state_path, "snapshot_path"),
            (snapshot.artifacts.memory_path, "mem_file_path"),
        ]
        for required_path, label in required_artifacts:
            if required_path is None:
                if snapshot.backend == BACKEND_QEMU and label != "disk_path":
                    continue
                raise SmolVMError(
                    f"Snapshot restore requires {label} to exist",
                    {"snapshot_id": snapshot_id, label: None},
                )
            if not required_path.exists():
                raise SmolVMError(
                    f"Snapshot restore requires {label} to exist",
                    {"snapshot_id": snapshot_id, label: str(required_path)},
                )

        managed_disk_path = self._instance_disk_path(restore_vm_id, snapshot.backend)
        persisted_vm_config = snapshot.vm_config.model_copy(
            update={
                "rootfs_path": managed_disk_path,
                "backend": snapshot.backend,
            }
        )
        restore_disk_path = managed_disk_path
        if snapshot.backend == BACKEND_QEMU:
            restore_disk_path = self._restore_staging_disk_path(managed_disk_path)
        restore_vm_config = persisted_vm_config
        if restore_disk_path != managed_disk_path:
            restore_vm_config = persisted_vm_config.model_copy(
                update={"rootfs_path": restore_disk_path}
            )
        effective_snapshot = snapshot.model_copy(update={"vm_config": restore_vm_config})
        adapter = self._runtime_adapter_for_snapshot(effective_snapshot)
        created_vm_record = False
        existing_disk_backup_path: Path | None = None
        launch = None

        managed_disk_path.parent.mkdir(parents=True, exist_ok=True)
        if restore_disk_path != managed_disk_path:
            restore_disk_path.parent.mkdir(parents=True, exist_ok=True)
            if restore_disk_path.exists():
                restore_disk_path.unlink()
        if managed_disk_path.exists():
            existing_disk_backup_path = self._restore_backup_disk_path(managed_disk_path)
            os.replace(managed_disk_path, existing_disk_backup_path)
        managed_disk_path.touch(exist_ok=True)

        try:
            if existing_vm is None:
                self.state.create_vm(persisted_vm_config)
                created_vm_record = True
            if effective_snapshot.backend == BACKEND_FIRECRACKER:
                self.state.allocate_ip(
                    restore_vm_id,
                    effective_snapshot.network_config.tap_device,
                    requested_ip=effective_snapshot.network_config.guest_ip,
                )
            if effective_snapshot.network_config.ssh_host_port is not None:
                self.state.reserve_ssh_port(
                    restore_vm_id,
                    host_port=effective_snapshot.network_config.ssh_host_port,
                )
            self.state.update_vm(restore_vm_id, network=effective_snapshot.network_config)
            if effective_snapshot.backend == BACKEND_FIRECRACKER:
                self._ensure_firecracker_network_for_restore(
                    restore_vm_id,
                    effective_snapshot.network_config,
                    vm_config=effective_snapshot.vm_config,
                )
            log_path = self.data_dir / f"{restore_vm_id}.log"
            launch = adapter.restore_snapshot(
                SnapshotRestoreRequest(
                    snapshot=effective_snapshot,
                    managed_disk_path=restore_disk_path,
                    log_path=log_path,
                    resume_vm=resume_vm,
                    boot_timeout=30.0,
                )
            )
            if restore_disk_path != managed_disk_path:
                os.replace(restore_disk_path, managed_disk_path)
            vm_info = self.state.update_vm(
                restore_vm_id,
                status=launch.status,
                pid=launch.pid,
                control_socket_path=launch.control_socket_path,
            )
            self.state.mark_snapshot_restored(snapshot_id, restore_vm_id)
            if existing_disk_backup_path is not None and existing_disk_backup_path.exists():
                with suppress(Exception):
                    existing_disk_backup_path.unlink()
            return vm_info
        except Exception:
            if launch is not None:
                with suppress(Exception):
                    adapter.stop(
                        VMInfo(
                            vm_id=restore_vm_id,
                            status=launch.status,
                            config=persisted_vm_config,
                            network=effective_snapshot.network_config,
                            pid=launch.pid,
                            control_socket_path=launch.control_socket_path,
                        ),
                        timeout=5.0,
                    )
            if created_vm_record:
                with suppress(Exception):
                    self._cleanup_resources(restore_vm_id)
                with suppress(Exception):
                    self.state.delete_vm(restore_vm_id)
            elif existing_vm is not None:
                self.state.update_vm(
                    restore_vm_id,
                    status=VMState.ERROR,
                    clear_pid=True,
                    clear_socket_path=True,
                )
                self._close_runtime_log(
                    restore_vm_id,
                    effective_snapshot.backend,
                    existing_vm.control_socket_path if existing_vm else None,
                )
            if restore_disk_path != managed_disk_path and restore_disk_path.exists():
                with suppress(Exception):
                    restore_disk_path.unlink()
            if existing_disk_backup_path is not None and existing_disk_backup_path.exists():
                with suppress(Exception):
                    if managed_disk_path.exists():
                        managed_disk_path.unlink()
                    os.replace(existing_disk_backup_path, managed_disk_path)
            raise

    def delete_snapshot(self, snapshot_id: str) -> None:
        """Delete snapshot files and metadata."""
        snapshot_root = self._snapshot_root_for_id(snapshot_id)

        snapshot = self.state.get_snapshot(snapshot_id)
        if snapshot.restored and snapshot.restored_vm_id:
            with suppress(VMNotFoundError):
                restored_vm = self.state.get_vm(snapshot.restored_vm_id)
                if restored_vm.status in (VMState.RUNNING, VMState.PAUSED):
                    raise SmolVMError(
                        "Cannot delete snapshot while restored VM is active",
                        {
                            "snapshot_id": snapshot_id,
                            "restored_vm_id": snapshot.restored_vm_id,
                        },
                    )

        with suppress(FileNotFoundError):
            shutil.rmtree(snapshot_root)
        self.state.delete_snapshot(snapshot_id)

    def delete(self, vm_id: str) -> None:
        """Delete a VM and all its resources.

        Args:
            vm_id: The VM identifier.

        Raises:
            VMNotFoundError: If VM doesn't exist.
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        logger.info("Deleting VM: %s", vm_id)

        # Stop if running
        try:
            vm_info = self.state.get_vm(vm_id)
        except VMNotFoundError:
            # Best-effort cleanup of leaked artifacts, then propagate the lookup error.
            self._cleanup_resources(vm_id)
            raise

        if vm_info.status in (VMState.RUNNING, VMState.PAUSED):
            self.stop(vm_id)

        # Cleanup all resources
        self._cleanup_resources(vm_id)

        # Delete from database
        self.state.delete_vm(vm_id)

        logger.info("VM deleted: %s", vm_id)

    def get(self, vm_id: str) -> VMInfo:
        """Get VM information.

        Args:
            vm_id: The VM identifier.

        Returns:
            VMInfo for the VM.

        Raises:
            VMNotFoundError: If VM doesn't exist.
        """
        return self.state.get_vm(vm_id)

    def list_vms(self, status: VMState | None = None) -> list[VMInfo]:
        """List all VMs.

        Args:
            status: Filter by status (optional).

        Returns:
            List of VMInfo objects.
        """
        return self.state.list_vms(status)

    def get_snapshot(self, snapshot_id: str) -> SnapshotInfo:
        """Get snapshot metadata."""
        return self.state.get_snapshot(snapshot_id)

    def list_snapshots(self, vm_id: str | None = None) -> list[SnapshotInfo]:
        """List snapshots, optionally filtered by source VM ID."""
        return self.state.list_snapshots(vm_id=vm_id)

    def refresh_status(self, vm_info: VMInfo) -> VMInfo:
        """Detect a dead VM process and demote the DB row to ERROR.

        Cheap per-row liveness check (one ``os.kill(pid, 0)`` syscall) for
        callers that already have a ``VMInfo`` in hand and don't want the
        full reconcile sweep. Returns ``vm_info`` unchanged if the VM is
        not in a status that should track a process, or if the process is
        still alive.
        """
        if vm_info.status not in (VMState.RUNNING, VMState.PAUSED):
            return vm_info
        if vm_info.pid is None:
            return vm_info
        if self._is_process_running(vm_info.pid):
            return vm_info
        logger.warning("VM %s pid %d is gone; marking ERROR.", vm_info.vm_id, vm_info.pid)
        return self.state.update_vm(vm_info.vm_id, status=VMState.ERROR, clear_pid=True)

    def reconcile(self) -> list[str]:
        """Reconcile state with actual system state.

        Detects and marks stale VMs (marked running but process dead).

        Returns:
            List of VM IDs that were marked as ERROR.
        """
        stale_vm_ids = self.state.reconcile()

        # If a VM was marked stale, remove any SSH forwarding rules so host
        # ports do not continue to route into dead guests.
        for vm_id in stale_vm_ids:
            with suppress(Exception):
                vm_info = self.state.get_vm(vm_id)
                if vm_info.network and vm_info.network.ssh_host_port is not None:
                    self.network.cleanup_ssh_port_forward(
                        vm_id=vm_id,
                        guest_ip=vm_info.network.guest_ip,
                        host_port=vm_info.network.ssh_host_port,
                    )

        return stale_vm_ids

    def get_ssh_commands(
        self,
        vm_id: str,
        *,
        ssh_user: str = "root",
        key_path: str | Path | None = None,
        public_host: str | None = None,
    ) -> dict[str, str]:
        """Return ready-to-run SSH commands for a VM.

        Returns a dictionary containing:
        - ``private_ip``: connect directly to guest IP from host
        - ``localhost_port``: connect via reserved forwarded host port
        - ``public``: connect from internet (only when ``public_host`` given)
        """
        if not vm_id:
            raise ValueError("vm_id cannot be empty")
        if not ssh_user:
            raise ValueError("ssh_user cannot be empty")

        vm_info = self.state.get_vm(vm_id)
        if vm_info.network is None:
            raise SmolVMError("VM has no network configuration", {"vm_id": vm_id})

        key_opt = ""
        if key_path is not None:
            key_opt = f"-i {shlex.quote(str(key_path))} "

        commands = {
            "private_ip": f"ssh {key_opt}{ssh_user}@{vm_info.network.guest_ip}".strip(),
        }

        if vm_info.network.ssh_host_port is not None:
            commands["localhost_port"] = (
                f"ssh {key_opt}-p {vm_info.network.ssh_host_port} {ssh_user}@127.0.0.1"
            ).strip()
            if public_host:
                commands["public"] = (
                    f"ssh {key_opt}-p {vm_info.network.ssh_host_port} {ssh_user}@{public_host}"
                ).strip()

        return commands

    def _start_qemu(
        self,
        vm_info: VMInfo,
        log_path: Path,
        *,
        control_socket_path: Path | None = None,
        start_paused: bool = False,
        root_node_name: str = QEMU_ROOT_NODE_NAME,
        firmware_vars_path: Path | None = None,
        swtpm_socket: Path | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start a QEMU process for the qemu backend.

        Thin shim: resolves the QEMU binary, the per-guest platform spec,
        and pre-computes boot args, then delegates argv assembly to
        :func:`smolvm.runtime.qemu_args.build_qemu_argv`. Spawning and
        process-tracking stay here.

        Args:
            vm_info: VM info with persisted configuration/network.
            log_path: Path for combined stdout/stderr log output.
            control_socket_path: Optional QMP control socket path.
            start_paused: Whether to start QEMU paused.
            root_node_name: QEMU block graph node name for the primary disk.
            firmware_vars_path: Per-VM OVMF NVRAM path; required for
                Windows guests. Passed in by the runtime adapter.
            swtpm_socket: Per-VM swtpm data-channel socket path; required
                for Windows guests. Spawned by the runtime adapter
                before invoking this shim.

        Returns:
            The started QEMU process.

        Raises:
            SmolVMError: If qemu binary or required config is missing.
        """
        qemu_bin = self._find_qemu_binary()
        if qemu_bin is None:
            raise SmolVMError(f"qemu-system binary is missing; {_qemu_install_hint()}")

        boot_args = self._resolve_boot_args(vm_info)
        platform_spec = get_guest_platform(
            vm_info.config.guest_os,
            host_system=platform.system(),
            arch=platform.machine(),
        )

        cmd = build_qemu_argv(
            vm_info,
            qemu_bin=qemu_bin,
            boot_args=boot_args,
            platform_spec=platform_spec,
            control_socket_path=control_socket_path,
            firmware_vars_path=firmware_vars_path,
            swtpm_socket=swtpm_socket,
            start_paused=start_paused,
            root_node_name=root_node_name,
        )

        logger.debug("Starting QEMU: %s", " ".join(cmd))

        log_file = open(log_path, "w")  # noqa: SIM115 - must stay open for subprocess

        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_file.close()
            raise

        key = f"qemu:{vm_info.vm_id}"
        self._log_files[key] = log_file
        self._process_handles[process.pid] = process

        logger.debug("Started QEMU: PID=%d, vm_id=%s", process.pid, vm_info.vm_id)
        return process

    def _start_firecracker(
        self,
        socket_path: Path,
        log_path: Path,
    ) -> subprocess.Popen[bytes]:
        """Start the Firecracker process.

        Args:
            socket_path: Path for the API socket.
            log_path: Path for log output.

        Returns:
            The Popen process object.

        Raises:
            SmolVMError: If firecracker binary is not found.
        """
        fc_path = self.host.find_firecracker()
        if fc_path is None:
            raise SmolVMError(
                "Firecracker binary not found. "
                "Install it with: smolvm.host.manager.HostManager().install_firecracker()"
            )

        cmd = [str(fc_path), "--api-sock", str(socket_path)]

        log_file = open(log_path, "w")  # noqa: SIM115 - must stay open for subprocess

        try:
            process = subprocess.Popen(
                cmd,
                # Detach child stdin from caller TTY so Firecracker cannot
                # mutate terminal/file flags seen by interactive REPL sessions.
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_file.close()
            raise

        # Derive a key from the socket path so we can close the handle later
        key = str(socket_path)
        self._log_files[key] = log_file
        self._process_handles[process.pid] = process

        logger.debug("Started Firecracker: PID=%d, socket=%s", process.pid, socket_path)
        return process

    def _find_krunvm_binary(self) -> Path | None:
        """Find an available ``krunvm`` binary."""
        return which("krunvm")

    def _start_libkrun(
        self,
        vm_info: VMInfo,
        log_path: Path,
    ) -> subprocess.Popen[bytes]:
        """Start a libkrun VM through the ``krunvm`` CLI."""
        krunvm_path = self._find_krunvm_binary()
        if krunvm_path is None:
            raise SmolVMError("libkrun backend requires krunvm to be installed and on PATH")

        cmd = [
            str(krunvm_path),
            "run",
            "--cpus",
            str(vm_info.config.vcpu_count),
            "--memory",
            str(vm_info.config.memory),
            "--kernel",
            str(vm_info.config.kernel_path),
            "--rootfs",
            str(vm_info.config.rootfs_path),
            "--kernel-params",
            self._resolve_boot_args(vm_info),
        ]
        if vm_info.config.initrd_path is not None:
            cmd.extend(["--initrd", str(vm_info.config.initrd_path)])
        if vm_info.config.vsock is not None:
            cmd.extend(["--vsock-cid", str(vm_info.config.vsock.guest_cid)])

        logger.debug("Starting libkrun: %s", " ".join(shlex.quote(part) for part in cmd))
        log_file = open(log_path, "w")  # noqa: SIM115 - must stay open for subprocess

        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_file.close()
            raise

        key = f"libkrun:{vm_info.vm_id}"
        self._log_files[key] = log_file
        self._process_handles[process.pid] = process
        logger.debug("Started libkrun: PID=%d, vm_id=%s", process.pid, vm_info.vm_id)
        return process

    def _unlink_socket(self, socket_path: Path) -> None:
        """Best-effort socket cleanup with sudo fallback for stale root-owned sockets."""
        try:
            socket_path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            # Old versions launched firecracker via sudo, producing root-owned sockets.
            # Try non-interactive sudo cleanup so retries can proceed.
            result = subprocess.run(
                ["sudo", "-n", "rm", "-f", str(socket_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                raise SmolVMError(
                    "Failed to remove stale Firecracker socket.\n"
                    f"Path: {socket_path}\n"
                    "Run one of the following:\n"
                    f"  sudo rm -f {socket_path}\n"
                    f"  {RUNTIME_PRIVILEGE_SETUP_HINT}\n"
                    f"sudo stderr: {stderr}"
                ) from None

    def _kill_process(self, pid: int) -> None:
        """Kill a process.

        Args:
            pid: Process ID to kill.
        """
        try:
            os.kill(pid, signal.SIGKILL)
            logger.debug("Killed process: %d", pid)
        except ProcessLookupError:
            pass  # Already dead
        except PermissionError:
            # Try with sudo
            try:
                subprocess.run(["sudo", "-n", "kill", "-9", str(pid)], check=False)
            except Exception:
                logger.warning("Failed to kill process %d", pid)

        handle = self._process_handles.get(pid)
        if handle is not None:
            with suppress(subprocess.TimeoutExpired):
                handle.wait(timeout=0.1)
                self._process_handles.pop(pid, None)

    def _is_process_running(self, pid: int) -> bool:
        """Check if a process is running.

        Args:
            pid: Process ID to check.

        Returns:
            True if running.
        """
        # Reap any zombie via the retained Popen handle so os.kill(pid, 0) below
        # cannot mistake a not-yet-reaped corpse for a live process.
        handle = self._process_handles.get(pid)
        if handle is not None and handle.poll() is not None:
            self._process_handles.pop(pid, None)
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # Can't signal but exists

    def _wait_for_process(self, pid: int, timeout: float) -> None:
        """Wait for a process to exit.

        Args:
            pid: Process ID to wait for.
            timeout: Maximum seconds to wait.
        """
        handle = self._process_handles.get(pid)
        if handle is not None:
            with suppress(subprocess.TimeoutExpired):
                handle.wait(timeout=timeout)
                self._process_handles.pop(pid, None)
            return

        start = time.time()
        while time.time() - start < timeout:
            if not self._is_process_running(pid):
                return
            time.sleep(0.1)

    def _close_runtime_log(
        self,
        vm_id: str,
        backend: str,
        control_socket_path: Path | None = None,
    ) -> None:
        """Close tracked runtime log handles for a VM."""
        keys: list[str] = []
        if backend == BACKEND_FIRECRACKER:
            socket_path = control_socket_path or (self.socket_dir / f"fc-{vm_id}.sock")
            keys.append(str(socket_path))
        elif backend == BACKEND_QEMU:
            keys.append(f"qemu:{vm_id}")
        elif backend == BACKEND_LIBKRUN:
            keys.append(f"libkrun:{vm_id}")

        for key in keys:
            fh = self._log_files.pop(key, None)
            if fh is not None:
                with suppress(Exception):
                    fh.close()

    def _resolve_boot_args(self, vm_info: VMInfo) -> str:
        """Resolve final boot args, injecting static IP config and SSH key when absent."""
        args = vm_info.config.boot_args.strip()
        parts = args.split()

        backend = self._backend_for_vm(vm_info)
        if backend in {BACKEND_QEMU, BACKEND_LIBKRUN}:
            # Firecracker defaults include pci=off, which breaks PCI-based desktop backends.
            parts = [part for part in parts if part != "pci=off"]
            if not any(part.startswith("root=") for part in parts):
                parts.extend(["root=/dev/vda", "rw"])
            args = " ".join(parts).strip()
            parts = args.split()

        ssh_public_key = vm_info.config.ssh_public_key
        if ssh_public_key and not any(
            part.startswith("smolvm.authorized_key_b64=") for part in parts
        ):
            # Base64-encode so the value is a single space-free token — SSH
            # public keys contain spaces ("ssh-ed25519 AAAA... user@host") that
            # would otherwise split into separate cmdline params.
            encoded = base64.b64encode(ssh_public_key.strip().encode("utf-8")).decode("ascii")
            args = f"{args} smolvm.authorized_key_b64={encoded}".strip()
            parts = args.split()

        if vm_info.network is None:
            return args

        if any(part.startswith("ip=") for part in parts):
            return args

        ip_arg = (
            f"ip={vm_info.network.guest_ip}::"
            f"{vm_info.network.gateway_ip}:{vm_info.network.netmask}::eth0:off"
        )
        return f"{args} {ip_arg}".strip()

    def _cleanup_resources(self, vm_id: str) -> None:
        """Clean up resources for a VM.

        Args:
            vm_id: The VM identifier.
        """
        try:
            vm_info = None
            with suppress(VMNotFoundError):
                vm_info = self.state.get_vm(vm_id)

            backend = self.backend
            if vm_info is not None:
                with suppress(SmolVMError):
                    backend = self._backend_for_vm(vm_info)

            lease = self.state.get_ip_lease(vm_id)

            ssh_host_port: int | None = None
            guest_ip: str | None = lease[0] if lease else None
            if vm_info and vm_info.network:
                ssh_host_port = vm_info.network.ssh_host_port
                guest_ip = vm_info.network.guest_ip
            else:
                ssh_host_port = self.state.get_ssh_port(vm_id)

            # Firecracker and QEMU-on-TAP both provision host TAP/NAT/ssh-forward
            # resources; slirp-mode QEMU and libkrun do not. When vm_info is gone
            # (early-failure cleanup) the IP lease is the definitive tell that a
            # TAP existed — `backend` is the manager default and may not match
            # the VM's real backend, so we can't rely on it here.
            if vm_info is not None:
                uses_tap = self._uses_host_tap_networking(vm_info.config, backend)
            else:
                uses_tap = lease is not None

            if uses_tap and ssh_host_port is not None and guest_ip:
                with suppress(Exception):
                    self.network.cleanup_ssh_port_forward(
                        vm_id=vm_id,
                        guest_ip=guest_ip,
                        host_port=ssh_host_port,
                    )

            # Reconnect flows may not have in-memory local-forward state.
            # Always remove any persisted localhost forwarding rules by vm_id.
            with suppress(Exception):
                self.network.cleanup_all_local_port_forwards(vm_id)

            # Get IP lease info
            if lease:
                _, tap_device = lease

                if uses_tap:
                    # Tear down host TAP/NAT for backends that provisioned it.
                    with suppress(Exception):
                        self.network.remove_egress_rules(tap_device)
                    self.network.cleanup_nat_rules(tap_device)
                    self.network.cleanup_tap(tap_device)

                # Release IP lease regardless of backend.
                self.state.release_ip(vm_id)

            if ssh_host_port is not None:
                self.state.release_ssh_port(vm_id)

            for socket_path in (
                self.socket_dir / f"fc-{vm_id}.sock",
                self.socket_dir / f"qmp-{vm_id}.sock",
            ):
                if socket_path.exists():
                    self._unlink_socket(socket_path)

            self._close_runtime_log(vm_id, BACKEND_FIRECRACKER)
            self._close_runtime_log(vm_id, BACKEND_QEMU)
            self._close_runtime_log(vm_id, BACKEND_LIBKRUN)

            managed_disk = self._managed_disk_for_vm(vm_info)
            if managed_disk and managed_disk.exists():
                if vm_info.config.retain_disk_on_delete:
                    logger.info(
                        "Retaining isolated disk for VM %s at %s",
                        vm_id,
                        managed_disk,
                    )
                else:
                    with suppress(Exception):
                        managed_disk.unlink()
                        logger.info("Removed isolated disk for VM %s: %s", vm_id, managed_disk)

            # Per-VM firmware state (OVMF NVRAM + swtpm). Coupled to the
            # disk lifecycle — kept iff retain_disk_on_delete is set so
            # Secure Boot enrollment persists for a later VM with the
            # same ID.
            firmware_state = self.data_dir / "firmware" / vm_id
            if firmware_state.exists():
                retain = vm_info is not None and vm_info.config.retain_disk_on_delete
                if retain:
                    logger.info(
                        "Retaining per-VM firmware state for VM %s at %s",
                        vm_id,
                        firmware_state,
                    )
                else:
                    with suppress(Exception):
                        shutil.rmtree(firmware_state)
                        logger.info(
                            "Removed per-VM firmware state for VM %s: %s",
                            vm_id,
                            firmware_state,
                        )

        except Exception as e:
            logger.warning("Error during cleanup for %s: %s", vm_id, e)

    # ==================================================================
    # Async lifecycle methods
    #
    # Each async method mirrors its sync counterpart, replacing blocking
    # subprocess calls and time.sleep() with asyncio equivalents.
    # ==================================================================

    async def async_create(self, config: VMConfig) -> VMInfo:
        """Async version of :meth:`create`."""
        if config is None:
            raise ValueError("config cannot be None")

        backend = self._backend_for_config(config)
        effective_config = config
        if effective_config.backend != backend:
            effective_config = effective_config.model_copy(update={"backend": backend})

        effective_config = await self._async_materialize_rootfs(effective_config)
        # Firmware materialization is a small file copy — synchronous is fine.
        self._materialize_firmware(effective_config)

        logger.info(
            "Creating VM (async): %s (backend=%s, disk_mode=%s)",
            effective_config.vm_id,
            backend,
            effective_config.disk_mode,
        )

        self.state.create_vm(effective_config)

        try:
            ssh_host_port = self._reserve_available_ssh_port(effective_config.vm_id)

            if not self._uses_host_tap_networking(effective_config, backend):
                if (
                    effective_config.internet_settings is not None
                    and not effective_config.internet_settings.is_allow_all_domains
                ):
                    logger.warning(
                        "internet_settings domain allowlist is not supported "
                        "with the %s backend (user-mode networking)",
                        backend,
                    )
                mac_seed = (ssh_host_port % 65534) + 1
                guest_mac = self.network.generate_mac(mac_seed)
                network_config = NetworkConfig(
                    guest_ip=QEMU_GUEST_IP,
                    gateway_ip=QEMU_GATEWAY_IP,
                    netmask=QEMU_NETMASK,
                    tap_device="usernet",
                    guest_mac=guest_mac,
                    ssh_host_port=ssh_host_port,
                )
                vm_info = self.state.update_vm(effective_config.vm_id, network=network_config)
                vm_info = self._maybe_enable_vsock(effective_config, backend, vm_info)
                return vm_info

            # Firecracker networking (async)
            guest_ip = self.state.allocate_ip(effective_config.vm_id, "pending")
            vm_number = ip_to_pool_index(guest_ip)
            tap_name = f"tap{vm_number}"
            self.state.update_ip_lease_tap(effective_config.vm_id, tap_name)

            user = os.environ.get("USER", "root")
            await self.network.async_create_tap(tap_name, user)
            await self.network.async_configure_tap(tap_name, netmask="32")
            await self.network.async_add_route(guest_ip, tap_name)
            await self.network.async_setup_nat(tap_name)

            # Apply domain allowlist if configured
            if (
                effective_config.internet_settings is not None
                and not effective_config.internet_settings.is_allow_all_domains
            ):
                allowed_ips = resolve_domains_to_ips(
                    effective_config.internet_settings.allowed_domains
                )
                await self.network.async_apply_egress_allowlist(tap_name, allowed_ips)

            await self.network.async_setup_ssh_port_forward(
                vm_id=effective_config.vm_id,
                guest_ip=guest_ip,
                host_port=ssh_host_port,
            )

            guest_mac = self.network.generate_mac(vm_number)
            network_config = NetworkConfig(
                guest_ip=guest_ip,
                gateway_ip=self.network.host_ip,
                tap_device=tap_name,
                guest_mac=guest_mac,
                ssh_host_port=ssh_host_port,
            )
            vm_info = self.state.update_vm(effective_config.vm_id, network=network_config)

            # QEMU-on-TAP still uses the vsock control channel (see the sync
            # create path for the rationale). No-op for Firecracker.
            if backend == BACKEND_QEMU:
                vm_info = self._maybe_enable_vsock(effective_config, backend, vm_info)

            return vm_info

        except Exception as e:
            logger.error("Failed to create VM %s: %s", effective_config.vm_id, e)
            await self._async_cleanup_resources(effective_config.vm_id)
            with suppress(Exception):
                self.state.delete_vm(effective_config.vm_id)
            raise

    async def async_start(
        self,
        vm_id: str,
        boot_timeout: float = 30.0,
    ) -> VMInfo:
        """Async version of :meth:`start`."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        logger.info("Starting VM (async): %s", vm_id)

        vm_info = self.state.get_vm(vm_id)

        if vm_info.status not in (VMState.CREATED, VMState.STOPPED):
            raise SmolVMError(
                f"Cannot start VM in state '{vm_info.status.value}'",
                {"vm_id": vm_id, "current_status": vm_info.status.value},
            )

        if vm_info.network is None:
            raise SmolVMError("VM has no network configuration", {"vm_id": vm_id})

        self._check_workspace_mounts(vm_info)

        backend = self._backend_for_vm(vm_info)
        if backend == BACKEND_QEMU:
            self._check_qemu_slirp_host_forward_ports(vm_info)
        log_path = self.data_dir / f"{vm_id}.log"
        adapter = self._runtime_adapter_for_backend(backend)
        try:
            launch = await adapter.async_start(
                vm_info, log_path=log_path, boot_timeout=boot_timeout
            )
            vm_info = self.state.update_vm(
                vm_id,
                status=launch.status,
                pid=launch.pid,
                control_socket_path=launch.control_socket_path,
            )
            logger.info(
                "VM started (async): %s (backend=%s, PID: %d)",
                vm_id,
                backend,
                launch.pid,
            )
            return vm_info
        except Exception as e:
            logger.error("Failed to start VM %s: %s", vm_id, e)
            self.state.update_vm(
                vm_id,
                status=VMState.ERROR,
                clear_pid=True,
                clear_socket_path=True,
            )
            self._close_runtime_log(vm_id, backend, vm_info.control_socket_path)
            raise

    async def async_stop(self, vm_id: str, timeout: float = 10.0) -> VMInfo:
        """Async version of :meth:`stop`."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        logger.info("Stopping VM (async): %s", vm_id)

        vm_info = self.state.get_vm(vm_id)

        if vm_info.status not in (VMState.RUNNING, VMState.PAUSED):
            logger.warning("VM %s is not running (status: %s)", vm_id, vm_info.status)
            return vm_info

        backend = self._backend_for_vm(vm_info)
        await self._runtime_adapter_for_backend(backend).async_stop(vm_info, timeout=timeout)
        self._close_runtime_log(vm_id, backend, vm_info.control_socket_path)
        vm_info = self.state.update_vm(
            vm_id,
            status=VMState.STOPPED,
            clear_pid=True,
            clear_socket_path=True,
        )
        logger.info("VM stopped (async): %s (backend=%s)", vm_id, backend)
        return vm_info

    async def async_delete(self, vm_id: str) -> None:
        """Async version of :meth:`delete`."""
        if not vm_id:
            raise ValueError("vm_id cannot be empty")

        logger.info("Deleting VM (async): %s", vm_id)

        try:
            vm_info = self.state.get_vm(vm_id)
        except VMNotFoundError:
            await self._async_cleanup_resources(vm_id)
            raise

        if vm_info.status in (VMState.RUNNING, VMState.PAUSED):
            await self.async_stop(vm_id)

        await self._async_cleanup_resources(vm_id)
        self.state.delete_vm(vm_id)
        logger.info("VM deleted (async): %s", vm_id)

    # ------------------------------------------------------------------
    # Async private helpers
    # ------------------------------------------------------------------

    async def _async_materialize_rootfs(self, config: VMConfig) -> VMConfig:
        """Async version of :meth:`_materialize_rootfs`."""
        if config.disk_mode == "shared":
            return config

        backend = self._backend_for_config(config)
        instance_rootfs = self._instance_disk_path(config.vm_id, backend)
        if not instance_rootfs.exists():
            logger.info(
                "Creating isolated disk (async) for VM %s from %s -> %s",
                config.vm_id,
                config.rootfs_path,
                instance_rootfs,
            )
            if backend == BACKEND_QEMU:
                await self._async_create_qemu_overlay_disk(config.rootfs_path, instance_rootfs)
            else:
                await self._async_copy_with_reflink(config.rootfs_path, instance_rootfs)

        return config.model_copy(update={"rootfs_path": instance_rootfs})

    async def _async_create_qemu_overlay_disk(self, base_path: Path, overlay_path: Path) -> None:
        """Async version of :meth:`_create_qemu_overlay_disk`."""
        from smolvm.utils import async_run_command

        qemu_img = self._find_qemu_img_binary()
        if qemu_img is None:
            raise SmolVMError("QEMU backend requires qemu-img to create overlay disks")

        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        base_format = "qcow2" if base_path.suffix == ".qcow2" else "raw"
        await async_run_command(
            [
                str(qemu_img),
                "create",
                "-f",
                "qcow2",
                "-b",
                str(base_path.resolve()),
                "-F",
                base_format,
                str(overlay_path),
            ],
            use_sudo=False,
        )

    async def _async_copy_with_reflink(self, source_path: Path, target_path: Path) -> None:
        """Async version of :meth:`_copy_with_reflink`."""
        import shutil

        from smolvm.utils import async_run_command

        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await async_run_command(
                ["cp", "--reflink=auto", str(source_path), str(target_path)],
                use_sudo=False,
            )
        except SmolVMError:
            await asyncio.to_thread(shutil.copy2, source_path, target_path)

    async def _async_unlink_socket(self, socket_path: Path) -> None:
        """Async version of :meth:`_unlink_socket`."""
        from smolvm.utils import async_run_command

        try:
            socket_path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            result = await async_run_command(
                ["rm", "-f", str(socket_path)],
                check=False,
            )
            if result.returncode != 0:
                raise SmolVMError(f"Failed to remove stale socket: {socket_path}") from None

    async def _async_start_libkrun(
        self,
        vm_info: VMInfo,
        log_path: Path,
    ) -> asyncio.subprocess.Process:
        """Async version of :meth:`_start_libkrun`."""
        import asyncio

        krunvm_path = self._find_krunvm_binary()
        if krunvm_path is None:
            raise SmolVMError("libkrun backend requires krunvm to be installed and on PATH")

        cmd = [
            str(krunvm_path),
            "run",
            "--cpus",
            str(vm_info.config.vcpu_count),
            "--memory",
            str(vm_info.config.memory),
            "--kernel",
            str(vm_info.config.kernel_path),
            "--rootfs",
            str(vm_info.config.rootfs_path),
            "--kernel-params",
            self._resolve_boot_args(vm_info),
        ]
        if vm_info.config.initrd_path is not None:
            cmd.extend(["--initrd", str(vm_info.config.initrd_path)])
        if vm_info.config.vsock is not None:
            cmd.extend(["--vsock-cid", str(vm_info.config.vsock.guest_cid)])

        logger.debug("Async starting libkrun: %s", " ".join(shlex.quote(part) for part in cmd))
        log_file = open(log_path, "w")  # noqa: SIM115 - must stay open while process runs

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            log_file.close()
            raise

        key = f"libkrun:{vm_info.vm_id}"
        self._log_files[key] = log_file
        logger.debug("Async started libkrun: PID=%d, vm_id=%s", process.pid, vm_info.vm_id)

        async def _close_log_when_done() -> None:
            try:
                await process.wait()
            finally:
                try:
                    log_file.close()
                finally:
                    self._log_files.pop(key, None)

        task = asyncio.create_task(_close_log_when_done())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return process

    async def _async_cleanup_resources(self, vm_id: str) -> None:
        """Async version of :meth:`_cleanup_resources`."""
        try:
            vm_info = None
            with suppress(VMNotFoundError):
                vm_info = self.state.get_vm(vm_id)

            backend = self.backend
            if vm_info is not None:
                with suppress(SmolVMError):
                    backend = self._backend_for_vm(vm_info)

            lease = self.state.get_ip_lease(vm_id)

            ssh_host_port: int | None = None
            guest_ip: str | None = lease[0] if lease else None
            if vm_info and vm_info.network:
                ssh_host_port = vm_info.network.ssh_host_port
                guest_ip = vm_info.network.guest_ip
            else:
                ssh_host_port = self.state.get_ssh_port(vm_id)

            # See the sync cleanup path for the TAP-vs-slirp rationale. When
            # vm_info is gone the IP lease is the definitive tell a TAP existed.
            if vm_info is not None:
                uses_tap = self._uses_host_tap_networking(vm_info.config, backend)
            else:
                uses_tap = lease is not None

            if uses_tap and ssh_host_port is not None and guest_ip:
                with suppress(Exception):
                    await self.network.async_cleanup_ssh_port_forward(
                        vm_id=vm_id,
                        guest_ip=guest_ip,
                        host_port=ssh_host_port,
                    )

            with suppress(Exception):
                await self.network.async_cleanup_all_local_port_forwards(vm_id)

            if lease:
                _, tap_device = lease

                if uses_tap:
                    with suppress(Exception):
                        await self.network.async_remove_egress_rules(tap_device)
                    await self.network.async_cleanup_nat_rules(tap_device)
                    await self.network.async_cleanup_tap(tap_device)

                self.state.release_ip(vm_id)

            if ssh_host_port is not None:
                self.state.release_ssh_port(vm_id)

            for socket_path in (
                self.socket_dir / f"fc-{vm_id}.sock",
                self.socket_dir / f"qmp-{vm_id}.sock",
            ):
                if socket_path.exists():
                    await self._async_unlink_socket(socket_path)

            self._close_runtime_log(vm_id, BACKEND_FIRECRACKER)
            self._close_runtime_log(vm_id, BACKEND_QEMU)
            self._close_runtime_log(vm_id, BACKEND_LIBKRUN)

            managed_disk = self._managed_disk_for_vm(vm_info)
            if managed_disk and managed_disk.exists():
                if vm_info.config.retain_disk_on_delete:
                    logger.info("Retaining isolated disk for VM %s at %s", vm_id, managed_disk)
                else:
                    with suppress(Exception):
                        managed_disk.unlink()
                        logger.info("Removed isolated disk for VM %s: %s", vm_id, managed_disk)

            # Per-VM firmware state (OVMF NVRAM + swtpm). Mirrors the sync
            # _cleanup_resources path so async_delete() doesn't leave
            # Windows-guest firmware behind.
            firmware_state = self.data_dir / "firmware" / vm_id
            if firmware_state.exists():
                retain = vm_info is not None and vm_info.config.retain_disk_on_delete
                if retain:
                    logger.info(
                        "Retaining per-VM firmware state for VM %s at %s",
                        vm_id,
                        firmware_state,
                    )
                else:
                    with suppress(Exception):
                        shutil.rmtree(firmware_state)
                        logger.info(
                            "Removed per-VM firmware state for VM %s: %s",
                            vm_id,
                            firmware_state,
                        )

        except Exception as e:
            logger.warning("Error during async cleanup for %s: %s", vm_id, e)
