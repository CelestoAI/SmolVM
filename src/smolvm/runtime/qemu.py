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

"""QEMU runtime adapter with QMP-backed control and snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from smolvm.exceptions import SmolVMError
from smolvm.qmp import QMPClient
from smolvm.runtime.backends import BACKEND_QEMU
from smolvm.runtime.base import (
    RuntimeAdapter,
    RuntimeContext,
    RuntimeLaunch,
    SnapshotCreateRequest,
    SnapshotCreateResult,
    SnapshotRestoreRequest,
)
from smolvm.runtime.guest_platforms import GuestPlatformSpec, get_guest_platform
from smolvm.types import GuestOS, SnapshotArtifacts, SnapshotType, VMInfo, VMState
from smolvm.utils import which

logger = logging.getLogger(__name__)

QEMU_ROOT_NODE_NAME = "rootdisk0"


def _qemu_img_install_hint() -> str:
    """Return a short recovery hint for missing or broken qemu-img."""
    return (
        "Install it with 'sudo apt-get install -y qemu-utils' on Debian/Ubuntu, "
        "'sudo dnf install -y qemu-img' or 'sudo yum install -y qemu-img' "
        "on Fedora/RHEL, or 'sudo pacman -S --needed qemu-base' on Arch; "
        "then verify 'qemu-img' is on PATH."
    )


class _SwtpmSidecar:
    """Per-VM swtpm (software TPM 2.0) process.

    Owned by :class:`QemuRuntimeAdapter`. Linux-host only — swtpm is a
    Linux-native daemon. The adapter spawns this before QEMU and tears
    it down after, passing the data-channel socket into the QEMU command
    line as ``-chardev socket,id=chrtpm,path=...``.

    Statelessness: the sidecar carries no in-memory bookkeeping. Each
    ``start`` / ``stop`` call rederives paths from ``vm_id`` and the
    runtime context, so the adapter can construct a new sidecar instance
    for a different lifecycle phase without coordination.
    """

    def __init__(
        self,
        *,
        vm_id: str,
        firmware_dir: Path,
        context: RuntimeContext,
    ) -> None:
        self._vm_id = vm_id
        self._state_dir = firmware_dir / vm_id / "swtpm"
        self._socket_path = self._state_dir / "swtpm-sock"
        self._pidfile = self._state_dir / "swtpm.pid"
        self._context = context

    @property
    def socket_path(self) -> Path:
        """Path to the swtpm data-channel Unix socket QEMU connects to."""
        return self._socket_path

    @property
    def pidfile_path(self) -> Path:
        """Path to swtpm's pidfile (used by the adapter for liveness checks)."""
        return self._pidfile

    def start(self, *, timeout: float = 5.0) -> int:
        """Spawn ``swtpm socket --tpm2 --daemon ...`` and return its pid.

        Raises:
            SmolVMError: When ``swtpm`` isn't on PATH, when the swtpm
                process exits non-zero, when the data socket never
                appears within *timeout* seconds, or when the pidfile
                isn't written.
        """
        # Clean any stale state from a previous run.
        with suppress(FileNotFoundError):
            self._socket_path.unlink()
        with suppress(FileNotFoundError):
            self._pidfile.unlink()
        self._state_dir.mkdir(parents=True, exist_ok=True)

        swtpm_bin = which("swtpm")
        if swtpm_bin is None:
            raise SmolVMError(
                "Windows guests need the swtpm software TPM emulator. "
                "Install it with 'sudo apt-get install -y swtpm swtpm-tools' "
                "on Debian/Ubuntu, 'sudo dnf install -y swtpm swtpm-tools' "
                "on Fedora/RHEL, or 'sudo pacman -S --needed swtpm' on Arch."
            )

        try:
            subprocess.run(
                [
                    str(swtpm_bin),
                    "socket",
                    "--tpmstate",
                    f"dir={self._state_dir}",
                    "--ctrl",
                    f"type=unixio,path={self._socket_path}",
                    "--tpm2",
                    "--daemon",
                    "--pid",
                    f"file={self._pidfile}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise SmolVMError(
                "Failed to start swtpm sidecar for Windows guest",
                {
                    "vm_id": self._vm_id,
                    "stderr": (exc.stderr or "").strip(),
                },
            ) from exc

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._socket_path.exists():
                break
            time.sleep(0.05)
        if not self._socket_path.exists():
            raise SmolVMError(
                "swtpm spawned but its control socket never appeared",
                {"socket_path": str(self._socket_path), "timeout": timeout},
            )
        if not self._pidfile.exists():
            raise SmolVMError(
                "swtpm spawned but no pidfile was written",
                {"pidfile": str(self._pidfile)},
            )
        return int(self._pidfile.read_text().strip())

    def stop(self) -> None:
        """Stop the swtpm daemon and remove the socket + pidfile.

        Best-effort: missing files are ignored; a process that's already
        gone is fine. The adapter calls this after stopping QEMU and on
        QEMU startup failure.
        """
        if self._pidfile.exists():
            try:
                pid = int(self._pidfile.read_text().strip())
            except ValueError:
                pid = -1
            if pid > 0 and self._context.is_process_running(pid):
                with suppress(ProcessLookupError, OSError):
                    os.kill(pid, signal.SIGTERM)
                deadline = time.time() + 5.0
                while time.time() < deadline and self._context.is_process_running(pid):
                    time.sleep(0.05)
                if self._context.is_process_running(pid):
                    with suppress(ProcessLookupError, OSError):
                        os.kill(pid, signal.SIGKILL)
        with suppress(FileNotFoundError):
            self._pidfile.unlink()
        with suppress(FileNotFoundError):
            self._socket_path.unlink()


class QemuRuntimeAdapter(RuntimeAdapter):
    """Hypervisor control for the QEMU backend."""

    backend = BACKEND_QEMU

    def __init__(self, context: RuntimeContext) -> None:
        self._context = context

    def start(self, vm_info: VMInfo, *, log_path: Path, boot_timeout: float) -> RuntimeLaunch:
        """Start QEMU (and a swtpm sidecar if the guest OS needs one)."""
        platform_spec = self._resolve_platform_spec(vm_info)

        control_socket_path = self._control_socket_path(vm_info.vm_id)
        if control_socket_path.exists():
            self._context.unlink_socket(control_socket_path)

        firmware_vars_path = self._firmware_vars_path(vm_info, platform_spec)
        swtpm_sidecar: _SwtpmSidecar | None = None
        if platform_spec.requires_swtpm:
            swtpm_sidecar = _SwtpmSidecar(
                vm_id=vm_info.vm_id,
                firmware_dir=self._context.firmware_dir,
                context=self._context,
            )
            swtpm_sidecar.start()

        process: Any | None = None
        try:
            process = self._context.start_qemu(
                vm_info,
                log_path,
                control_socket_path=control_socket_path,
                start_paused=False,
                root_node_name=QEMU_ROOT_NODE_NAME,
                firmware_vars_path=firmware_vars_path,
                swtpm_socket=(swtpm_sidecar.socket_path if swtpm_sidecar else None),
            )
            self._wait_for_runtime(process, control_socket_path, boot_timeout)
            return RuntimeLaunch(
                pid=process.pid,
                control_socket_path=control_socket_path,
                status=VMState.RUNNING,
            )
        except Exception:
            if process is not None:
                with suppress(Exception):
                    self._context.kill_process(process.pid)
            if control_socket_path.exists():
                with suppress(Exception):
                    self._context.unlink_socket(control_socket_path)
            if swtpm_sidecar is not None:
                with suppress(Exception):
                    swtpm_sidecar.stop()
            raise

    def stop(self, vm_info: VMInfo, *, timeout: float) -> None:
        """Stop a QEMU VM process and any swtpm sidecar it owns."""
        if vm_info.pid and self._context.is_process_running(vm_info.pid):
            try:
                os.kill(vm_info.pid, signal.SIGTERM)
                self._context.wait_for_process(vm_info.pid, timeout)
            except (OSError, SmolVMError):
                # Best-effort graceful shutdown failed; fall back to hard kill below.
                ...

        if vm_info.pid and self._context.is_process_running(vm_info.pid):
            self._context.kill_process(vm_info.pid)
            self._context.wait_for_process(vm_info.pid, min(timeout, 5.0))

        if vm_info.pid and self._context.is_process_running(vm_info.pid):
            raise SmolVMError(
                f"QEMU process did not exit for VM '{vm_info.vm_id}'",
                {"pid": vm_info.pid},
            )

        # Tear down the swtpm sidecar after QEMU is gone. We rederive
        # whether one ran from vm_info.config.guest_os so the stop path
        # has no in-memory dependency on what start() captured.
        if vm_info.config.guest_os is GuestOS.WINDOWS:
            sidecar = _SwtpmSidecar(
                vm_id=vm_info.vm_id,
                firmware_dir=self._context.firmware_dir,
                context=self._context,
            )
            with suppress(Exception):
                sidecar.stop()

        if vm_info.control_socket_path and vm_info.control_socket_path.exists():
            self._context.unlink_socket(vm_info.control_socket_path)

    @staticmethod
    def _resolve_platform_spec(vm_info: VMInfo) -> GuestPlatformSpec:
        """Resolve the guest platform spec for *vm_info*.

        Surfaces the macOS-host + Windows-guest rejection (and any other
        host-incompatibility raised by the spec factory) as a ``SmolVMError``
        with the VM ID in context, so the runtime adapter's error path
        is consistent with the rest of the SmolVM SDK.
        """
        try:
            return get_guest_platform(
                vm_info.config.guest_os,
                host_system=platform.system(),
                arch=platform.machine(),
            )
        except (NotImplementedError, ValueError) as exc:
            raise SmolVMError(
                str(exc),
                {"vm_id": vm_info.vm_id, "guest_os": vm_info.config.guest_os.value},
            ) from exc

    def _firmware_vars_path(
        self,
        vm_info: VMInfo,
        platform_spec: GuestPlatformSpec,
    ) -> Path | None:
        """Resolve the per-VM OVMF VARS path, when the guest needs one."""
        if platform_spec.firmware is None:
            return None
        return self._context.firmware_dir / vm_info.vm_id / "OVMF_VARS.fd"

    def pause(self, vm_info: VMInfo) -> None:
        """Pause a running QEMU VM."""
        with self._client(vm_info.control_socket_path) as client:
            client.stop_vm()

    def resume(self, vm_info: VMInfo) -> None:
        """Resume a paused QEMU VM."""
        with self._client(vm_info.control_socket_path) as client:
            client.cont()

    def create_snapshot(self, request: SnapshotCreateRequest) -> SnapshotCreateResult:
        """Create a QEMU snapshot and copy the managed qcow2 disk artifact."""
        vm_info = request.vm_info
        # DISK snapshots store only the block device — no guest RAM (vmstate) —
        # so they use the synchronous disk-only internal-snapshot primitive
        # instead of the heavyweight ``snapshot-save`` job that dumps memory.
        disk_only = request.snapshot_type == SnapshotType.DISK
        snapshot_saved = False
        with self._client(vm_info.control_socket_path) as client:
            if request.original_status == VMState.RUNNING:
                client.stop_vm()

            try:
                if disk_only:
                    client.blockdev_snapshot_internal_sync(QEMU_ROOT_NODE_NAME, request.snapshot_id)
                else:
                    save_job_id = f"snapshot-save-{request.snapshot_id}"
                    client.snapshot_save(
                        save_job_id,
                        request.snapshot_id,
                        QEMU_ROOT_NODE_NAME,
                        [QEMU_ROOT_NODE_NAME],
                    )
                    client.wait_for_job(save_job_id)
                snapshot_saved = True

                disk_path = request.snapshot_root / "disk.qcow2"
                if request.snapshot_type == SnapshotType.DIFF:
                    self._copy_disk_overlay(request.managed_disk_path, disk_path)
                else:
                    self._copy_disk_standalone(request.managed_disk_path, disk_path)

                if disk_only:
                    client.blockdev_snapshot_delete_internal_sync(
                        QEMU_ROOT_NODE_NAME, request.snapshot_id
                    )
                else:
                    delete_job_id = f"snapshot-delete-{request.snapshot_id}"
                    client.snapshot_delete(
                        delete_job_id, request.snapshot_id, [QEMU_ROOT_NODE_NAME]
                    )
                    client.wait_for_job(delete_job_id)

                return SnapshotCreateResult(
                    artifacts=SnapshotArtifacts(disk_path=disk_path),
                    source_status=VMState.PAUSED,
                )
            except Exception:
                if snapshot_saved:
                    with suppress(Exception):
                        if disk_only:
                            client.blockdev_snapshot_delete_internal_sync(
                                QEMU_ROOT_NODE_NAME, request.snapshot_id
                            )
                        else:
                            cleanup_job_id = f"snapshot-cleanup-{request.snapshot_id}"
                            client.snapshot_delete(
                                cleanup_job_id,
                                request.snapshot_id,
                                [QEMU_ROOT_NODE_NAME],
                            )
                            client.wait_for_job(cleanup_job_id)
                if request.original_status == VMState.RUNNING:
                    with suppress(Exception):
                        client.cont()
                raise

    def restore_snapshot(self, request: SnapshotRestoreRequest) -> RuntimeLaunch:
        """Restore a QEMU snapshot from a copied managed qcow2 disk."""
        snapshot = request.snapshot
        effective_config = snapshot.vm_config.model_copy(
            update={"rootfs_path": request.managed_disk_path}
        )
        vm_info = VMInfo(
            vm_id=snapshot.vm_id,
            status=VMState.CREATED,
            config=effective_config,
            network=snapshot.network_config,
        )

        request.managed_disk_path.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.snapshot_type == SnapshotType.DIFF:
            shutil.copy2(snapshot.artifacts.disk_path, request.managed_disk_path)
            backing = self._qcow2_backing_file(request.managed_disk_path)
            if backing is not None and not backing.exists():
                raise SmolVMError(
                    "This space-saving snapshot needs its original base image, "
                    f"which is missing: '{backing}'. Restore that file and run "
                    f"'smolvm snapshot restore {snapshot.snapshot_id}' again, or take "
                    "a full snapshot next time with '--snapshot-type full'.",
                    {"snapshot_id": snapshot.snapshot_id, "backing_file": str(backing)},
                )
        else:
            self._copy_disk_standalone(snapshot.artifacts.disk_path, request.managed_disk_path)

        control_socket_path = self._control_socket_path(snapshot.vm_id)
        if control_socket_path.exists():
            self._context.unlink_socket(control_socket_path)

        # A DISK snapshot carries no guest RAM, so there is nothing to load —
        # the guest boots fresh from the restored disk rather than resuming the
        # exact running state. We still start paused so the caller controls when
        # the cold boot begins (via ``resume_vm``).
        disk_only = snapshot.snapshot_type == SnapshotType.DISK
        process: Any | None = None
        try:
            process = self._context.start_qemu(
                vm_info,
                request.log_path,
                control_socket_path=control_socket_path,
                start_paused=True,
                root_node_name=QEMU_ROOT_NODE_NAME,
            )
            self._wait_for_runtime(process, control_socket_path, request.boot_timeout)

            with self._client(control_socket_path) as client:
                if not disk_only:
                    load_job_id = f"snapshot-load-{snapshot.snapshot_id}"
                    client.snapshot_load(
                        load_job_id,
                        snapshot.snapshot_id,
                        QEMU_ROOT_NODE_NAME,
                        [QEMU_ROOT_NODE_NAME],
                    )
                    client.wait_for_job(load_job_id)

                    delete_job_id = f"snapshot-delete-{snapshot.snapshot_id}"
                    client.snapshot_delete(
                        delete_job_id, snapshot.snapshot_id, [QEMU_ROOT_NODE_NAME]
                    )
                    client.wait_for_job(delete_job_id)

                if request.resume_vm:
                    client.cont()

            return RuntimeLaunch(
                pid=process.pid,
                control_socket_path=control_socket_path,
                status=VMState.RUNNING if request.resume_vm else VMState.PAUSED,
            )
        except Exception:
            if process is not None:
                with suppress(Exception):
                    self._context.kill_process(process.pid)
            if control_socket_path.exists():
                with suppress(Exception):
                    self._context.unlink_socket(control_socket_path)
            raise

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

    def _wait_for_runtime(self, process: Any, control_socket_path: Path, timeout: float) -> None:
        """Wait for QEMU to expose its QMP socket or fail fast if it exits."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            exit_code = process.poll()
            if exit_code is not None:
                raise SmolVMError(
                    f"QEMU exited early while booting VM '{getattr(process, 'pid', 'unknown')}'",
                    {"exit_code": exit_code},
                )
            try:
                with self._client(control_socket_path, timeout=0.2):
                    return
            except (OSError, SmolVMError):
                time.sleep(0.05)

        raise SmolVMError(
            "Timed out waiting for QEMU control socket",
            {"socket_path": str(control_socket_path)},
        )

    def _control_socket_path(self, vm_id: str) -> Path:
        """Return the persistent QMP socket path for a VM."""
        return self._context.socket_dir / f"qmp-{vm_id}.sock"

    @staticmethod
    def _local_backing_copy_path(root: Path, backing: Path, depth: int) -> Path:
        """Return a sidecar path for a copied backing file."""
        return root.with_name(f"{root.name}.backing-{depth}{backing.suffix or '.img'}")

    @staticmethod
    def _qcow2_backing_file_required(disk: Path) -> Path | None:
        """Return backing path, raising when qemu-img cannot inspect the disk."""
        qemu_img = which("qemu-img")
        if qemu_img is None:
            raise SmolVMError(
                f"QEMU snapshots need qemu-img to inspect qcow2 disks. {_qemu_img_install_hint()}"
            )
        info = subprocess.run(
            [str(qemu_img), "info", "-U", "--output=json", str(disk)],
            capture_output=True,
            text=True,
            check=False,
        )
        if info.returncode != 0:
            raise SmolVMError(
                "qemu-img could not inspect the QEMU disk while creating a full snapshot. "
                f"Confirm the disk is valid, or reinstall qemu-img. {_qemu_img_install_hint()}",
                {"disk_path": str(disk), "stderr": info.stderr.strip()},
            )
        try:
            data = json.loads(info.stdout)
        except (ValueError, TypeError) as exc:
            raise SmolVMError(
                "qemu-img returned invalid disk info while creating a full QEMU snapshot. "
                f"Update or reinstall qemu-img. {_qemu_img_install_hint()}",
                {"disk_path": str(disk)},
            ) from exc
        backing = data.get("full-backing-filename") or data.get("backing-filename")
        return Path(backing) if backing else None

    @staticmethod
    def _copy_disk_standalone(source: Path, dest: Path, *, _depth: int = 0) -> None:
        """Copy a qcow2 disk with a local backing chain.

        QEMU full snapshots store VM state as an internal qcow2 snapshot on the
        active disk. ``qemu-img convert`` would flatten an overlay, but it also
        drops those internal snapshot tags. Instead, copy the active overlay as
        is and re-point it at copied backing files next to *dest*. Restores then
        depend only on files under the managed disk directory, not on the
        snapshot directory.
        """
        backing = QemuRuntimeAdapter._qcow2_backing_file_required(source)
        if backing is None:
            shutil.copy2(source, dest)
            return
        if not backing.exists():
            raise SmolVMError(
                "Full snapshot needs the disk's base image, but that file is missing: "
                f"'{backing}'. Restore it, or take a diff snapshot instead."
            )

        shutil.copy2(source, dest)
        backing_dest = QemuRuntimeAdapter._local_backing_copy_path(dest, backing, _depth)
        QemuRuntimeAdapter._copy_disk_standalone(backing, backing_dest, _depth=_depth + 1)

        qemu_img = which("qemu-img")
        if qemu_img is None:
            raise SmolVMError(
                "QEMU snapshots need qemu-img to rebase copied qcow2 backing files. "
                f"{_qemu_img_install_hint()}"
            )
        backing_fmt = QemuRuntimeAdapter._qcow2_disk_format(backing_dest) or (
            "qcow2" if backing_dest.suffix == ".qcow2" else "raw"
        )
        rebase = subprocess.run(
            [
                str(qemu_img),
                "rebase",
                "-u",
                "-b",
                str(backing_dest.resolve()),
                "-F",
                backing_fmt,
                str(dest),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if rebase.returncode != 0:
            raise SmolVMError(
                "qemu-img rebase failed while creating a full QEMU snapshot. "
                "Confirm qemu-img is usable and the backing image exists. "
                f"{_qemu_img_install_hint()}",
                {"stderr": rebase.stderr.strip()},
            )

    @staticmethod
    def _qcow2_backing_file(disk: Path) -> Path | None:
        """Return the absolute backing-file path of a qcow2 disk, if any.

        Returns ``None`` when the disk has no backing file or when its layout
        cannot be read (e.g. ``qemu-img`` is unavailable).
        """
        qemu_img = which("qemu-img")
        if qemu_img is None:
            return None
        info = subprocess.run(
            [str(qemu_img), "info", "--output=json", str(disk)],
            capture_output=True,
            text=True,
            check=False,
        )
        if info.returncode != 0:
            return None
        try:
            data = json.loads(info.stdout)
        except (ValueError, TypeError):
            return None
        backing = data.get("full-backing-filename") or data.get("backing-filename")
        return Path(backing) if backing else None

    @staticmethod
    def _qcow2_disk_format(disk: Path) -> str | None:
        """Return the image format qemu-img reports for *disk*, or ``None``.

        Used to pass an accurate ``-F`` (backing format) to ``qemu-img rebase``
        instead of guessing from the file extension. Returns ``None`` — letting
        the caller fall back to the extension heuristic — when qemu-img is
        unavailable, errors, or omits the field.
        """
        qemu_img = which("qemu-img")
        if qemu_img is None:
            return None
        info = subprocess.run(
            [str(qemu_img), "info", "--output=json", str(disk)],
            capture_output=True,
            text=True,
            check=False,
        )
        if info.returncode != 0:
            logger.warning(
                "qemu-img info failed for backing file %s; guessing its format "
                "from the extension instead: %s",
                disk,
                info.stderr.strip(),
            )
            return None
        try:
            return json.loads(info.stdout).get("format")
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _copy_disk_overlay(source: Path, dest: Path) -> None:
        """Copy a qcow2 overlay for a diff snapshot, keeping its backing chain.

        Unlike :meth:`_copy_disk_standalone`, this preserves the thin overlay's
        link to the shared base image, so the snapshot stores only the clusters
        that changed since the base — much smaller, at the cost of depending on
        the base image still being present at restore time. The copy keeps any
        QEMU internal VM-state snapshot the overlay carries.

        When the source has no backing file there is nothing to diff against,
        so this falls back to a self-contained copy.
        """
        backing = QemuRuntimeAdapter._qcow2_backing_file(source)
        if backing is None:
            logger.info(
                "qcow2 has no backing file; writing a full snapshot instead of diff: %s",
                source,
            )
            QemuRuntimeAdapter._copy_disk_standalone(source, dest)
            return

        shutil.copy2(source, dest)

        # Re-point the copied overlay at the base by absolute path so it
        # resolves from the snapshot directory regardless of the cwd.
        qemu_img = which("qemu-img")
        if qemu_img is None:
            return
        backing_fmt = QemuRuntimeAdapter._qcow2_disk_format(backing) or (
            "qcow2" if backing.suffix == ".qcow2" else "raw"
        )
        rebase = subprocess.run(
            [
                str(qemu_img),
                "rebase",
                "-u",
                "-b",
                str(backing.resolve()),
                "-F",
                backing_fmt,
                str(dest),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if rebase.returncode != 0:
            logger.warning(
                "qemu-img rebase failed for diff snapshot; backing path left as-is: %s",
                rebase.stderr.strip(),
            )

    def _client(self, control_socket_path: Path | None, timeout: float = 5.0) -> QMPClient:
        """Connect a QMP client for a runtime control socket."""
        if control_socket_path is None:
            raise SmolVMError("VM has no QMP socket path")
        client = QMPClient(control_socket_path)
        client.connect(timeout=timeout)
        return client
