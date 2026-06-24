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

"""Thin QMP wrapper around the native smolvm-core client."""

from __future__ import annotations

import importlib
import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smolvm.exceptions import SmolVMError

try:
    _smolvm_core = importlib.import_module("smolvm_core._smolvm_core")
    _NativeQMPClient: Any | None = getattr(_smolvm_core, "_QmpClient", None)
except Exception:  # pragma: no cover - depends on wheel import failure shape
    _NativeQMPClient = None


@dataclass(slots=True, frozen=True)
class QMPJob:
    """Normalized QMP job status."""

    job_id: str
    job_type: str
    status: str
    current_progress: int
    total_progress: int
    error: str | None = None


def _native_error_to_smolvm(exc: Exception, socket_path: Path) -> SmolVMError:
    """Convert private PyO3 QMP errors into the public SmolVMError shape."""
    message = str(exc)
    context: dict[str, Any] = {"socket_path": str(socket_path)}
    try:
        payload = json.loads(message)
    except (TypeError, ValueError):
        return SmolVMError(message, context)

    if isinstance(payload, dict):
        payload_message = payload.get("message")
        payload_context = payload.get("context")
        if isinstance(payload_message, str):
            message = payload_message
        if isinstance(payload_context, dict):
            context = payload_context
            context.setdefault("socket_path", str(socket_path))
    return SmolVMError(message, context)


class QMPClient:
    """Small synchronous QMP client over a Unix socket."""

    def __init__(self, socket_path: Path) -> None:
        """Initialize a client for the given QMP control socket."""
        if socket_path is None:
            raise ValueError("socket_path cannot be None")
        if _NativeQMPClient is None:
            raise SmolVMError(
                "QEMU control support is missing; "
                "run `uv sync --reinstall-package smolvm-core` and try again.",
                {"socket_path": str(socket_path)},
            )

        self.socket_path = socket_path
        self._native: Any = _NativeQMPClient(str(socket_path))

    def __enter__(self) -> QMPClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def connect(self, timeout: float = 5.0, read_timeout: float = 30.0) -> None:
        """Connect to the QMP socket and negotiate capabilities."""
        try:
            self._native.connect(timeout, read_timeout)
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def execute(
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Execute a QMP command and return the ``return`` payload."""
        arguments_json = json.dumps(arguments) if arguments else None
        try:
            result_json = self._native.execute(command, arguments_json)
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc
        return json.loads(result_json)

    def query_status(self) -> dict[str, Any]:
        """Query the current VM run state."""
        result = self.execute("query-status")
        if not isinstance(result, dict):
            raise SmolVMError("Unexpected QMP query-status response", {"result": result})
        return result

    def query_version(self) -> dict[str, Any]:
        """Query the runtime QEMU version."""
        result = self.execute("query-version")
        if not isinstance(result, dict):
            raise SmolVMError("Unexpected QMP query-version response", {"result": result})
        return result

    def stop_vm(self) -> None:
        """Pause guest execution."""
        try:
            self._native.stop_vm()
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def cont(self) -> None:
        """Resume guest execution."""
        try:
            self._native.cont()
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def snapshot_save(self, job_id: str, tag: str, vmstate: str, devices: list[str]) -> None:
        """Create a QEMU internal snapshot."""
        try:
            self._native.snapshot_save(job_id, tag, vmstate, devices)
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def snapshot_load(self, job_id: str, tag: str, vmstate: str, devices: list[str]) -> None:
        """Load a QEMU internal snapshot."""
        try:
            self._native.snapshot_load(job_id, tag, vmstate, devices)
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def snapshot_delete(self, job_id: str, tag: str, devices: list[str]) -> None:
        """Delete a QEMU internal snapshot."""
        try:
            self._native.snapshot_delete(job_id, tag, devices)
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def blockdev_snapshot_internal_sync(self, device: str, name: str) -> None:
        """Create a disk-only internal qcow2 snapshot, synchronously."""
        try:
            self._native.blockdev_snapshot_internal_sync(device, name)
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def blockdev_snapshot_delete_internal_sync(self, device: str, name: str) -> None:
        """Delete a disk-only internal qcow2 snapshot, synchronously."""
        try:
            self._native.blockdev_snapshot_delete_internal_sync(device, name)
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def query_jobs(self) -> list[QMPJob]:
        """Return normalized job status rows."""
        try:
            result = json.loads(self._native.query_jobs())
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc
        if not isinstance(result, list):
            raise SmolVMError("Unexpected QMP query-jobs response", {"result": result})
        return [QMPJob(**job) for job in result]

    def dismiss_job(self, job_id: str) -> None:
        """Dismiss a concluded QMP job."""
        try:
            self._native.dismiss_job(job_id)
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 0.1,
    ) -> QMPJob:
        """Wait until a QMP job reaches the concluded state."""
        try:
            result = json.loads(self._native.wait_for_job(job_id, timeout, poll_interval))
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc
        return QMPJob(**result)

    def close(self) -> None:
        """Close the native client."""
        with suppress(Exception):
            self._native.close()
