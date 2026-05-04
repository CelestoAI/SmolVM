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

"""libkrun runtime adapter (via ``krunvm`` process management)."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

from smolvm.exceptions import SmolVMError
from smolvm.runtime.backends import BACKEND_LIBKRUN
from smolvm.runtime.base import (
    RuntimeAdapter,
    RuntimeContext,
    RuntimeLaunch,
    SnapshotCreateRequest,
    SnapshotCreateResult,
    SnapshotRestoreRequest,
)
from smolvm.types import VMInfo, VMState


class LibkrunRuntimeAdapter(RuntimeAdapter):
    """Hypervisor control for the libkrun backend."""

    backend = BACKEND_LIBKRUN

    def __init__(self, context: RuntimeContext) -> None:
        self._context = context

    def start(self, vm_info: VMInfo, *, log_path: Path, boot_timeout: float) -> RuntimeLaunch:
        if self._context.start_libkrun is None:
            raise SmolVMError("libkrun launch function is not configured in runtime context")
        process = self._context.start_libkrun(vm_info, log_path)
        self._wait_for_runtime(process, boot_timeout)
        return RuntimeLaunch(
            pid=process.pid,
            control_socket_path=None,
            status=VMState.RUNNING,
        )

    def stop(self, vm_info: VMInfo, *, timeout: float) -> None:
        if vm_info.pid and self._context.is_process_running(vm_info.pid):
            self._context.kill_process(vm_info.pid)
            self._context.wait_for_process(vm_info.pid, timeout)

        if vm_info.pid and self._context.is_process_running(vm_info.pid):
            raise SmolVMError(
                f"libkrun process did not exit for VM '{vm_info.vm_id}'",
                {"pid": vm_info.pid},
            )

    def pause(self, vm_info: VMInfo) -> None:
        raise SmolVMError("libkrun backend does not support pause yet")

    def resume(self, vm_info: VMInfo) -> None:
        raise SmolVMError("libkrun backend does not support resume yet")

    def create_snapshot(self, request: SnapshotCreateRequest) -> SnapshotCreateResult:
        raise SmolVMError("libkrun backend does not support snapshots yet")

    def restore_snapshot(self, request: SnapshotRestoreRequest) -> RuntimeLaunch:
        raise SmolVMError("libkrun backend does not support snapshot restore yet")

    async def async_start(
        self, vm_info: VMInfo, *, log_path: Path, boot_timeout: float
    ) -> RuntimeLaunch:
        if self._context.async_start_libkrun:
            process = await self._context.async_start_libkrun(vm_info, log_path)
        else:
            if self._context.start_libkrun is None:
                raise SmolVMError("libkrun launch function is not configured in runtime context")
            process = await asyncio.to_thread(self._context.start_libkrun, vm_info, log_path)

        await self._async_wait_for_runtime(process, boot_timeout)
        return RuntimeLaunch(
            pid=process.pid,
            control_socket_path=None,
            status=VMState.RUNNING,
        )

    async def async_stop(self, vm_info: VMInfo, *, timeout: float) -> None:
        await asyncio.to_thread(self.stop, vm_info, timeout=timeout)

    def _wait_for_runtime(self, process: Any, boot_timeout: float) -> None:
        import time

        start = time.time()
        while time.time() - start < boot_timeout:
            if process.poll() is not None:
                raise SmolVMError("libkrun process exited before VM became ready")
            time.sleep(0.05)
            continue

        with suppress(Exception):
            self._context.kill_process(process.pid)
        raise SmolVMError(
            "Timed out waiting for libkrun runtime to become ready",
            {"timeout_seconds": boot_timeout, "pid": process.pid},
        )

    async def _async_wait_for_runtime(self, process: Any, boot_timeout: float) -> None:
        start = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - start < boot_timeout:
            if process.returncode is not None:
                raise SmolVMError("libkrun process exited before VM became ready")
            await asyncio.sleep(0.05)
            continue

        with suppress(Exception):
            self._context.kill_process(process.pid)
        raise SmolVMError(
            "Timed out waiting for libkrun runtime to become ready",
            {"timeout_seconds": boot_timeout, "pid": process.pid},
        )
