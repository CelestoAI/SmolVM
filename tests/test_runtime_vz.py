# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.macos.models import MacOSLaunchResult
from smolvm.runtime.base import RuntimeContext, SnapshotCreateRequest
from smolvm.runtime.vz import VzRuntimeAdapter
from smolvm.types import (
    DesktopEndpoint,
    GuestOS,
    MacOSMachineConfig,
    SnapshotCapturePolicy,
    SnapshotType,
    VMConfig,
    VMInfo,
    VMState,
)


def _vm(tmp_path: Path) -> VMInfo:
    return VMInfo(
        vm_id="mac-test",
        status=VMState.CREATED,
        config=VMConfig(
            vm_id="mac-test",
            guest_os=GuestOS.MACOS,
            backend="vz",
            boot_mode="platform",
            memory=8192,
            macos_machine=MacOSMachineConfig(
                base_image="macos-latest",
                manifest_path=tmp_path / "manifest.json",
                bundle_path=tmp_path / "storage" / "mac-test",
                guest_version="26.0",
            ),
        ),
    )


def _context(tmp_path: Path) -> RuntimeContext:
    context = MagicMock(spec=RuntimeContext)
    context.process_handles = {}
    return context


def test_vz_runtime_launches_display_and_tracks_process(tmp_path: Path) -> None:
    driver = MagicMock()
    process = MagicMock()
    process.pid = 4321
    endpoint = DesktopEndpoint(port=5901)
    driver.start.return_value = (
        process,
        MacOSLaunchResult(
            pid=4321,
            display=endpoint,
            ip_address="192.168.64.3",
            vnc_password="private-secret",
        ),
    )
    adapter = VzRuntimeAdapter(_context(tmp_path), driver=driver)

    launch = adapter.start(_vm(tmp_path), log_path=tmp_path / "vm.log", boot_timeout=10)

    assert launch.pid == 4321
    assert launch.status is VMState.RUNNING
    assert launch.display == endpoint
    assert adapter._context.process_handles[4321] is process
    password_file = tmp_path / "storage" / "mac-test" / ".smolvm-vnc-password"
    assert password_file.is_file()
    assert password_file.read_text().strip() == "private-secret"
    assert password_file.stat().st_mode & 0o077 == 0


def test_vz_runtime_rejects_snapshot_with_recovery(tmp_path: Path) -> None:
    vm = _vm(tmp_path)
    adapter = VzRuntimeAdapter(_context(tmp_path), driver=MagicMock())
    request = SnapshotCreateRequest(
        vm_info=vm,
        snapshot_id="snap-mac-test",
        snapshot_root=tmp_path / "snapshot",
        managed_disk_path=tmp_path / "disk",
        resume_source=False,
        original_status=VMState.STOPPED,
        snapshot_type=SnapshotType.DISK,
        capture_policy=SnapshotCapturePolicy.ALLOW_PAUSE,
    )

    with pytest.raises(SmolVMError, match="smolvm sandbox stop mac-test"):
        adapter.create_snapshot(request)
