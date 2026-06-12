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

"""End-to-end lifecycle of real sandboxes via the public ``SmolVM`` API.

The shared-sandbox tests are a deliberately *ordered, stateful* smoke: the one
VM from the ``vm`` fixture (booted once per variant) is walked through its
lifecycle, asserting one capability per step. An early failure (e.g. boot)
cascades — the signal we want from a smoke suite.

Sequence: start -> exec -> upload/download -> pause/resume -> stop/cleanup.

Snapshot/restore is the exception: ``restore_snapshot`` rebuilds the *original*
VM identity in place, which would corrupt a shared sandbox, so it runs against
its own throwaway VM at the end.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import suppress
from pathlib import Path

import pytest
from _util import (
    BOOT_TIMEOUT,
    E2E_BACKENDS,
    E2EBackend,
    require_backend_available,
    selected_backend,
)

from smolvm import SmolVM
from smolvm.exceptions import SmolVMError, VMNotFoundError
from smolvm.runtime.backends import BACKEND_FIRECRACKER, BACKEND_QEMU
from smolvm.types import SnapshotType, VMState

pytestmark = pytest.mark.e2e

# ---------------------------------------------------------------------------
# Shared sandbox: one VM (per transport), walked through its lifecycle.
# ---------------------------------------------------------------------------


def test_start(vm: SmolVM) -> None:
    """The fixture booted; the VM is running and ready to take commands."""
    assert vm.status == VMState.RUNNING
    assert vm.can_run_commands()


def test_exec(vm: SmolVM) -> None:
    """A command runs and its stdout comes back."""
    result = vm.run("echo hello")
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"


def test_exec_exit_code(vm: SmolVM) -> None:
    """Non-zero exit codes propagate faithfully (not just the happy path)."""
    result = vm.run("exit 7")
    assert result.exit_code == 7


def test_upload_download(vm: SmolVM, tmp_path: Path) -> None:
    """A file round-trips host -> guest -> host byte-for-byte."""
    payload = os.urandom(4096)
    src = tmp_path / "payload.bin"
    src.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    vm.upload_file(str(src), "/tmp/payload.bin")

    digest = vm.run("sha256sum /tmp/payload.bin")
    assert digest.exit_code == 0
    assert digest.stdout.split()[0] == expected

    dest = tmp_path / "roundtrip.bin"
    vm.download_file("/tmp/payload.bin", str(dest))
    assert dest.read_bytes() == payload


def test_pause_resume(vm: SmolVM) -> None:
    """Pause halts the VM; resume brings it back and it still runs commands."""
    vm.pause()
    assert vm.status == VMState.PAUSED

    vm.resume()
    assert vm.status == VMState.RUNNING
    assert vm.run("echo back").stdout.strip() == "back"


def test_stop_and_cleanup(vm: SmolVM) -> None:
    """Stop transitions to STOPPED; delete removes the VM for good.

    This is the shared sandbox's final step; the fixture teardown is just a
    safety net for earlier failures.
    """
    vm_id = vm.vm_id

    vm.stop()
    assert vm.status == VMState.STOPPED

    vm.delete()
    with pytest.raises(VMNotFoundError):
        SmolVM.from_id(vm_id)


# ---------------------------------------------------------------------------
# Snapshot / restore: standalone, own VM (restore rebuilds the VM in place).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param(
            BACKEND_QEMU,
            marks=pytest.mark.xfail(
                reason=(
                    "QEMU snapshot RESTORE is currently broken on the isolated-disk "
                    "overlay: loadvm reports \"Device 'rootdisk0-drive' is writable but "
                    'does not support snapshots". Snapshot *creation* works; restore needs '
                    "a runtime fix (attach the restored root disk as a snapshot-capable "
                    "qcow2 node). Remove this xfail once restore lands."
                ),
                raises=SmolVMError,
                strict=False,
            ),
        ),
        *[backend for backend in E2E_BACKENDS if backend != BACKEND_QEMU],
    ],
    ids=str,
)
def test_snapshot_restore(backend: E2EBackend, request: pytest.FixtureRequest) -> None:
    """Snapshot a VM, restore it, and confirm guest state survived.

    Self-contained: ``from_snapshot`` restores into the *original* vm_id, so a
    shared sandbox can't be used. We stop the source before restoring so the
    restore path doesn't race to kill a still-live runtime process.
    """
    selected = selected_backend(request.config)
    if selected != "all" and backend != selected:
        pytest.skip(
            f"End-to-end tests for '{backend}' are skipped because this run selected "
            f"'{selected}'; rerun all backends with: pytest tests/e2e."
        )
    require_backend_available(backend, request.config, sandbox_name=f"snapshot-{backend}")

    sandbox = SmolVM(backend=backend, os="alpine", comm_channel="ssh")
    restored: SmolVM | None = None
    try:
        sandbox.start(boot_timeout=BOOT_TIMEOUT)
        assert sandbox.run("echo sentinel-content > /root/sentinel.txt").exit_code == 0

        snapshot_type = SnapshotType.DISK if backend == BACKEND_FIRECRACKER else SnapshotType.FULL
        snap = sandbox.snapshot(snapshot_type=snapshot_type)
        sandbox.stop()
        sandbox.delete()

        restored = SmolVM.from_snapshot(snap.snapshot_id, backend=backend, resume_vm=True)
        result = restored.run("cat /root/sentinel.txt")
        assert result.exit_code == 0
        assert result.stdout.strip() == "sentinel-content"
    finally:
        # restored reuses the source vm_id, so deleting either removes the VM.
        target = restored or sandbox
        with suppress(Exception):
            target.stop()
        with suppress(Exception):
            target.delete()
