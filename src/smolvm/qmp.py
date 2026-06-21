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

"""Minimal QMP client for QEMU runtime control."""

from __future__ import annotations

import importlib
import json
import logging
import socket
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from smolvm.exceptions import SmolVMError

logger = logging.getLogger(__name__)

try:
    _smolvm_core = importlib.import_module("smolvm_core._smolvm_core")
    _NativeQMPClient: Any | None = getattr(_smolvm_core, "_QmpClient", None)
except Exception:  # pragma: no cover - depends on optional native wheel shape
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


class _PythonQMPClient:
    """Pure-Python QMP client fallback over a Unix socket."""

    def __init__(self, socket_path: Path) -> None:
        """Initialize a client for the given QMP control socket."""
        if socket_path is None:
            raise ValueError("socket_path cannot be None")

        self.socket_path = socket_path
        self._socket: socket.socket | None = None
        self._reader: Any | None = None
        self._writer: Any | None = None

    def __enter__(self) -> _PythonQMPClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def connect(self, timeout: float = 5.0, read_timeout: float = 30.0) -> None:
        """Connect to the QMP socket and negotiate capabilities.

        ``timeout`` bounds how long we keep retrying the initial connect while
        QEMU is still bringing the monitor socket up; each probe uses a short
        (≤1s) socket timeout so a not-yet-ready socket fails fast and retries.
        Once QMP greeting/capabilities negotiation succeeds, the socket timeout
        is raised to ``read_timeout`` so that replies to slow commands (e.g.
        ``snapshot-save`` while QEMU is busy
        dumping guest RAM, which can take far longer than 1s to even
        acknowledge) are not cut off mid-read with ``TimeoutError``.
        """
        if self._socket is not None:
            return

        deadline = time.time() + timeout
        while True:
            qmp_socket: socket.socket | None = None
            try:
                qmp_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                qmp_socket.settimeout(min(timeout, 1.0))
                qmp_socket.connect(str(self.socket_path))
                self._socket = qmp_socket
                self._reader = qmp_socket.makefile("r", encoding="utf-8")
                self._writer = qmp_socket.makefile("w", encoding="utf-8")
                break
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                if time.time() >= deadline:
                    raise SmolVMError(
                        "Timed out waiting for QMP socket",
                        {"socket_path": str(self.socket_path)},
                    ) from None
                with suppress(Exception):
                    if qmp_socket is not None:
                        qmp_socket.close()
                time.sleep(0.05)

        try:
            greeting = self._read_message()
            if "QMP" not in greeting:
                raise SmolVMError(
                    "Invalid QMP greeting",
                    {"socket_path": str(self.socket_path), "greeting": greeting},
                )
            self.execute("qmp_capabilities")
            assert self._socket is not None
            # Handshake complete: switch from the short probe timeout to a
            # generous read timeout for command replies.
            self._socket.settimeout(read_timeout)
        except Exception:
            self.close()
            raise

    def execute(
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Execute a QMP command and return the ``return`` payload."""
        if self._socket is None:
            self.connect()

        assert self._writer is not None
        payload: dict[str, Any] = {"execute": command}
        if arguments:
            payload["arguments"] = arguments

        self._writer.write(json.dumps(payload))
        self._writer.write("\n")
        self._writer.flush()

        while True:
            message = self._read_message()
            if "event" in message:
                logger.debug("Ignoring QMP event: %s", message)
                continue
            if "error" in message:
                error = message["error"]
                raise SmolVMError(
                    f"QMP command '{command}' failed",
                    {
                        "socket_path": str(self.socket_path),
                        "command": command,
                        "class": error.get("class"),
                        "desc": error.get("desc"),
                    },
                )
            if "return" in message:
                return message["return"]

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
        self.execute("stop")

    def cont(self) -> None:
        """Resume guest execution."""
        self.execute("cont")

    def snapshot_save(self, job_id: str, tag: str, vmstate: str, devices: list[str]) -> None:
        """Create a QEMU internal snapshot."""
        self.execute(
            "snapshot-save",
            {
                "job-id": job_id,
                "tag": tag,
                "vmstate": vmstate,
                "devices": devices,
            },
        )

    def snapshot_load(self, job_id: str, tag: str, vmstate: str, devices: list[str]) -> None:
        """Load a QEMU internal snapshot."""
        self.execute(
            "snapshot-load",
            {
                "job-id": job_id,
                "tag": tag,
                "vmstate": vmstate,
                "devices": devices,
            },
        )

    def snapshot_delete(self, job_id: str, tag: str, devices: list[str]) -> None:
        """Delete a QEMU internal snapshot."""
        self.execute(
            "snapshot-delete",
            {
                "job-id": job_id,
                "tag": tag,
                "devices": devices,
            },
        )

    def blockdev_snapshot_internal_sync(self, device: str, name: str) -> None:
        """Create a **disk-only** internal qcow2 snapshot, synchronously.

        Unlike ``snapshot-save``, this saves only the block device's data (no
        guest RAM / vmstate), so it is fast, never dumps memory, and does not go
        through the async job API — it returns once the consistent point is
        written. ``device`` is the block node-name (or qdev id); ``name`` is the
        internal snapshot tag.
        """
        self.execute(
            "blockdev-snapshot-internal-sync",
            {
                "device": device,
                "name": name,
            },
        )

    def blockdev_snapshot_delete_internal_sync(self, device: str, name: str) -> None:
        """Delete a disk-only internal qcow2 snapshot, synchronously."""
        self.execute(
            "blockdev-snapshot-delete-internal-sync",
            {
                "device": device,
                "name": name,
            },
        )

    def query_jobs(self) -> list[QMPJob]:
        """Return normalized job status rows."""
        result = self.execute("query-jobs")
        if not isinstance(result, list):
            raise SmolVMError("Unexpected QMP query-jobs response", {"result": result})
        return [
            QMPJob(
                job_id=str(job["id"]),
                job_type=str(job["type"]),
                status=str(job["status"]),
                current_progress=int(job.get("current-progress", 0)),
                total_progress=int(job.get("total-progress", 0)),
                error=str(job["error"]) if "error" in job else None,
            )
            for job in result
        ]

    def dismiss_job(self, job_id: str) -> None:
        """Dismiss a concluded QMP job."""
        self.execute("job-dismiss", {"id": job_id})

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 0.1,
    ) -> QMPJob:
        """Wait until a QMP job reaches the concluded state."""
        deadline = time.time() + timeout
        last_job: QMPJob | None = None
        while time.time() < deadline:
            for job in self.query_jobs():
                if job.job_id != job_id:
                    continue
                last_job = job
                if job.status == "concluded":
                    if job.error is not None:
                        raise SmolVMError(
                            "QMP job failed",
                            {
                                "socket_path": str(self.socket_path),
                                "job_id": job.job_id,
                                "job_type": job.job_type,
                                "status": job.status,
                                "error": job.error,
                            },
                        )
                    with suppress(Exception):
                        self.dismiss_job(job_id)
                    return job
                break
            time.sleep(poll_interval)

        raise SmolVMError(
            "Timed out waiting for QMP job",
            {
                "socket_path": str(self.socket_path),
                "job_id": job_id,
                "last_status": last_job.status if last_job else None,
            },
        )

    def close(self) -> None:
        """Close all file and socket handles."""
        for handle in (self._reader, self._writer):
            with suppress(Exception):
                if handle is not None:
                    handle.close()
        self._reader = None
        self._writer = None
        if self._socket is not None:
            with suppress(Exception):
                self._socket.close()
        self._socket = None

    def _read_message(self) -> dict[str, Any]:
        """Read a single QMP JSON message."""
        if self._reader is None:
            raise SmolVMError("QMP client is not connected", {"socket_path": str(self.socket_path)})

        line = self._reader.readline()
        if not line:
            raise SmolVMError(
                "QMP socket closed unexpectedly", {"socket_path": str(self.socket_path)}
            )
        message = json.loads(line)
        if not isinstance(message, dict):
            raise SmolVMError(
                "Invalid QMP message",
                {"socket_path": str(self.socket_path), "message": message},
            )
        return cast(dict[str, Any], message)


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

        self.socket_path = socket_path
        self._native: Any | None = (
            _NativeQMPClient(str(socket_path)) if _NativeQMPClient is not None else None
        )
        self._fallback: _PythonQMPClient | None = (
            None if self._native is not None else _PythonQMPClient(socket_path)
        )

    @property
    def _socket(self) -> socket.socket | None:
        """Expose fallback socket for legacy tests/debugging."""
        return self._fallback._socket if self._fallback is not None else None

    @property
    def _reader(self) -> Any | None:
        """Expose fallback reader for legacy tests/debugging."""
        return self._fallback._reader if self._fallback is not None else None

    @property
    def _writer(self) -> Any | None:
        """Expose fallback writer for legacy tests/debugging."""
        return self._fallback._writer if self._fallback is not None else None

    def __enter__(self) -> QMPClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def connect(self, timeout: float = 5.0, read_timeout: float = 30.0) -> None:
        """Connect to the QMP socket and negotiate capabilities."""
        if self._native is None:
            assert self._fallback is not None
            self._fallback.connect(timeout=timeout, read_timeout=read_timeout)
            return
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
        if self._native is None:
            assert self._fallback is not None
            return self._fallback.execute(command, arguments)

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
        if self._native is None:
            assert self._fallback is not None
            self._fallback.stop_vm()
            return
        try:
            self._native.stop_vm()
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def cont(self) -> None:
        """Resume guest execution."""
        if self._native is None:
            assert self._fallback is not None
            self._fallback.cont()
            return
        try:
            self._native.cont()
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def snapshot_save(self, job_id: str, tag: str, vmstate: str, devices: list[str]) -> None:
        """Create a QEMU internal snapshot."""
        if self._native is None:
            assert self._fallback is not None
            self._fallback.snapshot_save(job_id, tag, vmstate, devices)
            return
        try:
            self._native.snapshot_save(job_id, tag, vmstate, devices)
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def snapshot_load(self, job_id: str, tag: str, vmstate: str, devices: list[str]) -> None:
        """Load a QEMU internal snapshot."""
        if self._native is None:
            assert self._fallback is not None
            self._fallback.snapshot_load(job_id, tag, vmstate, devices)
            return
        try:
            self._native.snapshot_load(job_id, tag, vmstate, devices)
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def snapshot_delete(self, job_id: str, tag: str, devices: list[str]) -> None:
        """Delete a QEMU internal snapshot."""
        if self._native is None:
            assert self._fallback is not None
            self._fallback.snapshot_delete(job_id, tag, devices)
            return
        try:
            self._native.snapshot_delete(job_id, tag, devices)
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def blockdev_snapshot_internal_sync(self, device: str, name: str) -> None:
        """Create a disk-only internal qcow2 snapshot, synchronously."""
        if self._native is None:
            assert self._fallback is not None
            self._fallback.blockdev_snapshot_internal_sync(device, name)
            return
        try:
            self._native.blockdev_snapshot_internal_sync(device, name)
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def blockdev_snapshot_delete_internal_sync(self, device: str, name: str) -> None:
        """Delete a disk-only internal qcow2 snapshot, synchronously."""
        if self._native is None:
            assert self._fallback is not None
            self._fallback.blockdev_snapshot_delete_internal_sync(device, name)
            return
        try:
            self._native.blockdev_snapshot_delete_internal_sync(device, name)
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc

    def query_jobs(self) -> list[QMPJob]:
        """Return normalized job status rows."""
        if self._native is None:
            assert self._fallback is not None
            return self._fallback.query_jobs()
        try:
            result = json.loads(self._native.query_jobs())
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc
        if not isinstance(result, list):
            raise SmolVMError("Unexpected QMP query-jobs response", {"result": result})
        return [QMPJob(**job) for job in result]

    def dismiss_job(self, job_id: str) -> None:
        """Dismiss a concluded QMP job."""
        if self._native is None:
            assert self._fallback is not None
            self._fallback.dismiss_job(job_id)
            return
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
        if self._native is None:
            assert self._fallback is not None
            return self._fallback.wait_for_job(job_id, timeout=timeout, poll_interval=poll_interval)
        try:
            result = json.loads(self._native.wait_for_job(job_id, timeout, poll_interval))
        except Exception as exc:
            raise _native_error_to_smolvm(exc, self.socket_path) from exc
        return QMPJob(**result)

    def close(self) -> None:
        """Close all file and socket handles."""
        if self._native is None:
            assert self._fallback is not None
            self._fallback.close()
            return
        with suppress(Exception):
            self._native.close()

    def _read_message(self) -> dict[str, Any]:
        """Read a single QMP JSON message through the fallback client."""
        if self._fallback is None:
            raise SmolVMError(
                "QMP native client does not expose raw message reads",
                {"socket_path": str(self.socket_path)},
            )
        return self._fallback._read_message()
