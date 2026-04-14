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

import logging
import shutil
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
from smolvm.types import SnapshotArtifacts, VMInfo, VMState
from smolvm.utils import which

logger = logging.getLogger(__name__)

QEMU_ROOT_NODE_NAME = "rootdisk0"


class QemuRuntimeAdapter(RuntimeAdapter):
    """Hypervisor control for the QEMU backend."""

    backend = BACKEND_QEMU

    def __init__(self, context: RuntimeContext) -> None:
        self._context = context

    def start(self, vm_info: VMInfo, *, log_path: Path, boot_timeout: float) -> RuntimeLaunch:
        """Start QEMU with a persistent QMP socket."""
        control_socket_path = self._control_socket_path(vm_info.vm_id)
        if control_socket_path.exists():
            self._context.unlink_socket(control_socket_path)

        process: Any | None = None
        try:
            process = self._context.start_qemu(
                vm_info,
                log_path,
                control_socket_path=control_socket_path,
                start_paused=False,
                root_node_name=QEMU_ROOT_NODE_NAME,
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
            raise

    def stop(self, vm_info: VMInfo, *, timeout: float) -> None:
        """Stop a QEMU VM process."""
        if vm_info.pid and self._context.is_process_running(vm_info.pid):
            try:
                import os
                import signal

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

        if vm_info.control_socket_path and vm_info.control_socket_path.exists():
            self._context.unlink_socket(vm_info.control_socket_path)

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
        snapshot_saved = False
        with self._client(vm_info.control_socket_path) as client:
            if request.original_status == VMState.RUNNING:
                client.stop_vm()

            try:
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
                self._copy_disk_standalone(request.managed_disk_path, disk_path)

                delete_job_id = f"snapshot-delete-{request.snapshot_id}"
                client.snapshot_delete(delete_job_id, request.snapshot_id, [QEMU_ROOT_NODE_NAME])
                client.wait_for_job(delete_job_id)

                return SnapshotCreateResult(
                    artifacts=SnapshotArtifacts(disk_path=disk_path),
                    source_status=VMState.PAUSED,
                )
            except Exception:
                if snapshot_saved:
                    with suppress(Exception):
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
        shutil.copy2(snapshot.artifacts.disk_path, request.managed_disk_path)

        control_socket_path = self._control_socket_path(snapshot.vm_id)
        if control_socket_path.exists():
            self._context.unlink_socket(control_socket_path)

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
                load_job_id = f"snapshot-load-{snapshot.snapshot_id}"
                client.snapshot_load(
                    load_job_id,
                    snapshot.snapshot_id,
                    QEMU_ROOT_NODE_NAME,
                    [QEMU_ROOT_NODE_NAME],
                )
                client.wait_for_job(load_job_id)

                delete_job_id = f"snapshot-delete-{snapshot.snapshot_id}"
                client.snapshot_delete(delete_job_id, snapshot.snapshot_id, [QEMU_ROOT_NODE_NAME])
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

    # ------------------------------------------------------------------
    # Async lifecycle methods
    # ------------------------------------------------------------------

    async def async_start(
        self, vm_info: VMInfo, *, log_path: Path, boot_timeout: float
    ) -> RuntimeLaunch:
        """Async version of :meth:`start`."""
        import asyncio

        control_socket_path = self._control_socket_path(vm_info.vm_id)
        if control_socket_path.exists():
            if self._context.async_unlink_socket:
                await self._context.async_unlink_socket(control_socket_path)
            else:
                self._context.unlink_socket(control_socket_path)

        process: Any | None = None
        try:
            if self._context.async_start_qemu:
                process = await self._context.async_start_qemu(
                    vm_info,
                    log_path,
                    control_socket_path=control_socket_path,
                    start_paused=False,
                    root_node_name=QEMU_ROOT_NODE_NAME,
                )
            else:
                process = await asyncio.to_thread(
                    self._context.start_qemu,
                    vm_info,
                    log_path,
                    control_socket_path=control_socket_path,
                    start_paused=False,
                    root_node_name=QEMU_ROOT_NODE_NAME,
                )
            await self._async_wait_for_runtime(process, control_socket_path, boot_timeout)
            return RuntimeLaunch(
                pid=process.pid,
                control_socket_path=control_socket_path,
                status=VMState.RUNNING,
            )
        except Exception:
            if process is not None:
                with suppress(Exception):
                    if self._context.async_kill_process:
                        await self._context.async_kill_process(process.pid)
                    else:
                        self._context.kill_process(process.pid)
            if control_socket_path.exists():
                with suppress(Exception):
                    if self._context.async_unlink_socket:
                        await self._context.async_unlink_socket(control_socket_path)
                    else:
                        self._context.unlink_socket(control_socket_path)
            raise

    async def async_stop(self, vm_info: VMInfo, *, timeout: float) -> None:
        """Async version of :meth:`stop`."""
        import asyncio
        import os
        import signal

        if vm_info.pid and self._context.is_process_running(vm_info.pid):
            try:
                os.kill(vm_info.pid, signal.SIGTERM)
                if self._context.async_wait_for_process:
                    await self._context.async_wait_for_process(vm_info.pid, timeout)
                else:
                    await asyncio.to_thread(
                        self._context.wait_for_process, vm_info.pid, timeout
                    )
            except (OSError, SmolVMError):
                ...

        if vm_info.pid and self._context.is_process_running(vm_info.pid):
            if self._context.async_kill_process:
                await self._context.async_kill_process(vm_info.pid)
            else:
                self._context.kill_process(vm_info.pid)
            if self._context.async_wait_for_process:
                await self._context.async_wait_for_process(vm_info.pid, min(timeout, 5.0))
            else:
                await asyncio.to_thread(
                    self._context.wait_for_process, vm_info.pid, min(timeout, 5.0)
                )

        if vm_info.pid and self._context.is_process_running(vm_info.pid):
            raise SmolVMError(
                f"QEMU process did not exit for VM '{vm_info.vm_id}'",
                {"pid": vm_info.pid},
            )

        if vm_info.control_socket_path and vm_info.control_socket_path.exists():
            if self._context.async_unlink_socket:
                await self._context.async_unlink_socket(vm_info.control_socket_path)
            else:
                self._context.unlink_socket(vm_info.control_socket_path)

    async def _async_wait_for_runtime(
        self, process: Any, control_socket_path: Path, timeout: float
    ) -> None:
        """Async version of :meth:`_wait_for_runtime`."""
        import asyncio

        deadline = time.time() + timeout
        while time.time() < deadline:
            exit_code = process.returncode if hasattr(process, "returncode") else process.poll()
            if exit_code is not None:
                raise SmolVMError(
                    f"QEMU exited early while booting VM '{getattr(process, 'pid', 'unknown')}'",
                    {"exit_code": exit_code},
                )
            try:
                with self._client(control_socket_path, timeout=0.2):
                    return
            except SmolVMError:
                await asyncio.sleep(0.05)

        raise SmolVMError(
            "Timed out waiting for QEMU control socket",
            {"socket_path": str(control_socket_path)},
        )

    # ------------------------------------------------------------------
    # Sync lifecycle methods (original)
    # ------------------------------------------------------------------

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
            except SmolVMError:
                time.sleep(0.05)

        raise SmolVMError(
            "Timed out waiting for QEMU control socket",
            {"socket_path": str(control_socket_path)},
        )

    def _control_socket_path(self, vm_id: str) -> Path:
        """Return the persistent QMP socket path for a VM."""
        return self._context.socket_dir / f"qmp-{vm_id}.sock"

    @staticmethod
    def _copy_disk_standalone(source: Path, dest: Path) -> None:
        """Copy a qcow2 disk, flattening any backing-file chain.

        If the source has a backing file (i.e. it's a thin overlay), uses
        ``qemu-img convert`` to produce a self-contained qcow2. Otherwise
        falls back to a simple file copy.
        """
        qemu_img = which("qemu-img")
        if qemu_img is not None:
            # Check if the source has a backing file
            info_result = subprocess.run(
                [str(qemu_img), "info", "--output=json", str(source)],
                capture_output=True,
                text=True,
                check=False,
            )
            has_backing = (
                info_result.returncode == 0 and "backing-filename" in info_result.stdout
            )

            if has_backing:
                logger.info(
                    "Flattening overlay qcow2 for snapshot: %s -> %s",
                    source,
                    dest,
                )
                convert_result = subprocess.run(
                    [
                        str(qemu_img),
                        "convert",
                        "-f",
                        "qcow2",
                        "-O",
                        "qcow2",
                        str(source),
                        str(dest),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if convert_result.returncode == 0:
                    return
                logger.warning(
                    "qemu-img convert failed during snapshot, falling back to copy: %s",
                    convert_result.stderr.strip(),
                )

        shutil.copy2(source, dest)

    def _client(self, control_socket_path: Path | None, timeout: float = 5.0) -> QMPClient:
        """Connect a QMP client for a runtime control socket."""
        if control_socket_path is None:
            raise SmolVMError("VM has no QMP socket path")
        client = QMPClient(control_socket_path)
        client.connect(timeout=timeout)
        return client
