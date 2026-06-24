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

"""QMP wrapper that translates smolvm-core errors into SmolVM errors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from smolvm_core import errors as core_errors
from smolvm_core import qmp as core_qmp

from smolvm.exceptions import SmolVMError

QMPJob = core_qmp.QMPJob


def _core_error_to_smolvm(exc: Exception, socket_path: Path) -> SmolVMError:
    if isinstance(exc, core_errors.QMPError):
        details = dict(exc.details)
        details.setdefault("socket_path", str(socket_path))
        return SmolVMError(str(exc), details)
    return SmolVMError(str(exc), {"socket_path": str(socket_path)})


class QMPClient:
    """Small synchronous QMP client over a Unix socket."""

    def __init__(self, socket_path: Path) -> None:
        """Initialize a client for the given QMP control socket."""
        if socket_path is None:
            raise ValueError("socket_path cannot be None")
        self.socket_path = socket_path
        try:
            self._core = core_qmp.QMPClient(socket_path)
        except core_errors.CoreUnavailableError as exc:
            raise SmolVMError(
                "QEMU control support is missing; "
                "run `uv sync --reinstall-package smolvm-core` and try again.",
                {"socket_path": str(socket_path)},
            ) from exc
        except core_errors.SmolVMCoreError as exc:
            raise _core_error_to_smolvm(exc, socket_path) from exc

    def __enter__(self) -> QMPClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _call(self, method: str, *args: object, **kwargs: object) -> Any:
        try:
            return getattr(self._core, method)(*args, **kwargs)
        except core_errors.SmolVMCoreError as exc:
            raise _core_error_to_smolvm(exc, self.socket_path) from exc

    def connect(self, timeout: float = 5.0, read_timeout: float = 30.0) -> None:
        """Connect to the QMP socket and negotiate capabilities."""
        self._call("connect", timeout, read_timeout)

    def execute(
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Execute a QMP command and return the ``return`` payload."""
        return self._call("execute", command, arguments)

    def query_status(self) -> dict[str, Any]:
        """Query the current VM run state."""
        return self._call("query_status")

    def query_version(self) -> dict[str, Any]:
        """Query the runtime QEMU version."""
        return self._call("query_version")

    def stop_vm(self) -> None:
        """Pause guest execution."""
        self._call("stop_vm")

    def cont(self) -> None:
        """Resume guest execution."""
        self._call("cont")

    def snapshot_save(self, job_id: str, tag: str, vmstate: str, devices: list[str]) -> None:
        """Create a QEMU internal snapshot."""
        self._call("snapshot_save", job_id, tag, vmstate, devices)

    def snapshot_load(self, job_id: str, tag: str, vmstate: str, devices: list[str]) -> None:
        """Load a QEMU internal snapshot."""
        self._call("snapshot_load", job_id, tag, vmstate, devices)

    def snapshot_delete(self, job_id: str, tag: str, devices: list[str]) -> None:
        """Delete a QEMU internal snapshot."""
        self._call("snapshot_delete", job_id, tag, devices)

    def blockdev_snapshot_internal_sync(self, device: str, name: str) -> None:
        """Create a disk-only internal qcow2 snapshot, synchronously."""
        self._call("blockdev_snapshot_internal_sync", device, name)

    def blockdev_snapshot_delete_internal_sync(self, device: str, name: str) -> None:
        """Delete a disk-only internal qcow2 snapshot, synchronously."""
        self._call("blockdev_snapshot_delete_internal_sync", device, name)

    def query_jobs(self) -> list[QMPJob]:
        """Return normalized job status rows."""
        return self._call("query_jobs")

    def dismiss_job(self, job_id: str) -> None:
        """Dismiss a concluded QMP job."""
        self._call("dismiss_job", job_id)

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 0.1,
    ) -> QMPJob:
        """Wait until a QMP job reaches the concluded state."""
        return self._call("wait_for_job", job_id, timeout=timeout, poll_interval=poll_interval)

    def close(self) -> None:
        """Close the native client."""
        self._core.close()


__all__ = ["QMPClient", "QMPJob"]
