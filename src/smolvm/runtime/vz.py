# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Apple Virtualization.framework runtime adapter backed by pinned Lume."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from smolvm.exceptions import SmolVMError
from smolvm.host.lume import find_lume_binary, pinned_lume_ready
from smolvm.macos.driver import MacOSRuntimeDriver
from smolvm.macos.lume import LumeDriver
from smolvm.macos.models import MacOSRunRequest
from smolvm.runtime.backends import BACKEND_VZ
from smolvm.runtime.base import (
    RuntimeAdapter,
    RuntimeContext,
    RuntimeLaunch,
    SnapshotCreateRequest,
    SnapshotCreateResult,
    SnapshotRestoreRequest,
)
from smolvm.types import GuestOS, MacOSMachineConfig, VMInfo, VMState


class VzRuntimeAdapter(RuntimeAdapter):
    """Lifecycle control for local Apple Silicon macOS desktop VMs."""

    backend = BACKEND_VZ

    def __init__(
        self,
        context: RuntimeContext,
        driver: MacOSRuntimeDriver | None = None,
    ) -> None:
        self._context = context
        if driver is None:
            binary = find_lume_binary()
            if binary is None or not pinned_lume_ready():
                raise SmolVMError(
                    "The macOS sandbox runtime isn't installed. Run 'smolvm setup --macos', then "
                    "'smolvm doctor --backend vz' to confirm."
                )
            driver = LumeDriver(binary)
        self._driver = driver

    @staticmethod
    def _machine(vm_info: VMInfo) -> MacOSMachineConfig:
        if vm_info.config.guest_os is not GuestOS.MACOS or vm_info.config.macos_machine is None:
            raise SmolVMError(
                f"Sandbox '{vm_info.vm_id}' does not have a macOS machine bundle; delete it with "
                f"'smolvm sandbox delete {vm_info.vm_id}' and create it again with '--os macos'."
            )
        return vm_info.config.macos_machine

    @staticmethod
    def _vnc_password_path(bundle_path: Path) -> Path:
        return bundle_path / ".smolvm-vnc-password"

    def _write_vnc_password(self, bundle_path: Path, password: str) -> None:
        path = self._vnc_password_path(bundle_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{password}\n", encoding="utf-8")
        path.chmod(0o600)

    def start(self, vm_info: VMInfo, *, log_path: Path, boot_timeout: float) -> RuntimeLaunch:
        machine = self._machine(vm_info)
        process, result = self._driver.start(
            MacOSRunRequest(
                name=vm_info.vm_id,
                storage_path=machine.bundle_path.parent,
                workspace_mounts=tuple(vm_info.config.workspace_mounts),
            ),
            log_path=log_path,
            timeout=boot_timeout,
        )
        try:
            self._write_vnc_password(machine.bundle_path, result.vnc_password)
        except Exception:
            with suppress(Exception):
                self._driver.stop(
                    vm_info.vm_id,
                    storage_path=machine.bundle_path.parent,
                    timeout=10,
                )
            process.terminate()
            raise
        self._context.process_handles[process.pid] = process
        return RuntimeLaunch(
            pid=result.pid,
            control_socket_path=None,
            status=VMState.RUNNING,
            display=result.display,
        )

    def stop(self, vm_info: VMInfo, *, timeout: float) -> None:
        machine = self._machine(vm_info)
        self._driver.stop(vm_info.vm_id, storage_path=machine.bundle_path.parent, timeout=timeout)
        if vm_info.pid is not None:
            self._context.process_handles.pop(vm_info.pid, None)
        self._vnc_password_path(machine.bundle_path).unlink(missing_ok=True)

    def pause(self, vm_info: VMInfo) -> None:
        raise SmolVMError(
            f"macOS sandbox '{vm_info.vm_id}' cannot be paused in this release; run "
            f"'smolvm sandbox stop {vm_info.vm_id}' instead."
        )

    def resume(self, vm_info: VMInfo) -> None:
        raise SmolVMError(
            f"macOS sandbox '{vm_info.vm_id}' cannot be resumed in this release; run "
            f"'smolvm sandbox start {vm_info.vm_id}' instead."
        )

    def create_snapshot(self, request: SnapshotCreateRequest) -> SnapshotCreateResult:
        vm_id = request.vm_info.vm_id
        raise SmolVMError(
            f"macOS sandbox '{vm_id}' does not support snapshots in this release; stop it with "
            f"'smolvm sandbox stop {vm_id}' and keep the sandbox instead."
        )

    def restore_snapshot(self, request: SnapshotRestoreRequest) -> RuntimeLaunch:
        raise SmolVMError(
            f"macOS snapshot '{request.snapshot.snapshot_id}' cannot be restored in this release; "
            "create a new macOS sandbox instead."
        )

    async def async_start(
        self, vm_info: VMInfo, *, log_path: Path, boot_timeout: float
    ) -> RuntimeLaunch:
        return await asyncio.to_thread(
            self.start,
            vm_info,
            log_path=log_path,
            boot_timeout=boot_timeout,
        )

    async def async_stop(self, vm_info: VMInfo, *, timeout: float) -> None:
        await asyncio.to_thread(self.stop, vm_info, timeout=timeout)
