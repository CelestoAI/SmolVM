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

"""Host-side HTTP-over-vsock transport for the Rust SmolVM guest agent."""

from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import socket
import stat
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from smolvm.comm import protocol
from smolvm.comm.base import CommChannelKind, ShellMode
from smolvm.exceptions import OperationTimeoutError, SmolVMError
from smolvm.types import CommandResult

logger = logging.getLogger(__name__)

_READY_FAST_POLL_WINDOW = 1.0
_READY_FAST_POLL_INTERVAL = 0.02


class _SocketHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that uses a caller-supplied connected socket."""

    def __init__(self, open_socket: Callable[[], socket.socket], *, timeout: float) -> None:
        super().__init__("smolvm-guest-agent", timeout=timeout)
        self._open_socket = open_socket

    def connect(self) -> None:
        self.sock = self._open_socket()


class RustHttpVsockChannel:
    """Drive the Rust guest agent over HTTP on a vsock stream.

    The transport opens one HTTP/1.1 connection per request. It supports both
    native host ``AF_VSOCK`` (QEMU on Linux) and Firecracker's host-side Unix
    socket bridge, which expects a ``CONNECT <port>`` line before the vsock
    byte stream begins.
    """

    kind: CommChannelKind = "vsock"

    def __init__(
        self,
        *,
        guest_cid: int | None = None,
        uds_path: str | Path | None = None,
        agent_port: int = protocol.SMOLVM_AGENT_PORT,
        connect_timeout: int = 10,
    ) -> None:
        if (guest_cid is None) == (uds_path is None):
            raise ValueError("provide exactly one of guest_cid or uds_path")
        if connect_timeout < 1:
            raise ValueError("connect_timeout must be >= 1")
        self.guest_cid = guest_cid
        self.uds_path = str(uds_path) if uds_path is not None else None
        self.agent_port = agent_port
        self.connect_timeout = connect_timeout
        self._ready = False

    @classmethod
    def from_cid(
        cls,
        guest_cid: int,
        *,
        agent_port: int = protocol.SMOLVM_AGENT_PORT,
        connect_timeout: int = 10,
    ) -> RustHttpVsockChannel:
        return cls(guest_cid=guest_cid, agent_port=agent_port, connect_timeout=connect_timeout)

    @classmethod
    def from_uds(
        cls,
        uds_path: str | Path,
        *,
        agent_port: int = protocol.SMOLVM_AGENT_PORT,
        connect_timeout: int = 10,
    ) -> RustHttpVsockChannel:
        return cls(uds_path=uds_path, agent_port=agent_port, connect_timeout=connect_timeout)

    def _open(self) -> socket.socket:
        if self.uds_path is not None:
            return self._open_uds()
        return self._open_vsock()

    def _open_vsock(self) -> socket.socket:
        if not hasattr(socket, "AF_VSOCK"):
            raise SmolVMError(
                "vsock is not available on this host (no AF_VSOCK). "
                "Use the SSH channel, or run on a Linux host with vhost_vsock loaded."
            )
        sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        sock.settimeout(float(self.connect_timeout))
        try:
            sock.connect((self.guest_cid, self.agent_port))
        except OSError:
            sock.close()
            raise
        return sock

    def _open_uds(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(float(self.connect_timeout))
        try:
            sock.connect(self.uds_path)
            sock.sendall(f"CONNECT {self.agent_port}\n".encode())
            ack = self._read_line(sock)
            if not ack.startswith("OK"):
                raise SmolVMError(f"vsock CONNECT handshake failed: {ack!r}")
        except Exception:
            sock.close()
            raise
        return sock

    @staticmethod
    def _read_line(sock: socket.socket) -> str:
        buf = bytearray()
        while not buf.endswith(b"\n"):
            byte = sock.recv(1)
            if not byte:
                break
            buf.extend(byte)
        return buf.decode("utf-8", errors="replace").strip()

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json", "Connection": "close"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn = _SocketHTTPConnection(
            self._open,
            timeout=float(timeout if timeout is not None else self.connect_timeout),
        )
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status >= 400:
                detail = data.decode("utf-8", errors="replace")
                raise SmolVMError(f"guest agent HTTP {resp.status} for {method} {path}: {detail}")
            decoded = json.loads(data.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise SmolVMError("guest agent returned a non-object JSON response")
            return decoded
        except TimeoutError as exc:
            raise OperationTimeoutError(
                f"guest agent request: {method} {path}", timeout or 0
            ) from exc
        finally:
            conn.close()

    def run(
        self,
        command: str,
        timeout: int = 30,
        shell: ShellMode = "login",
    ) -> CommandResult:
        if not command or not command.strip():
            raise ValueError("command cannot be empty")
        if timeout < 1:
            raise ValueError("timeout must be >= 1")
        resp = self._request_json(
            "POST",
            "/exec",
            {"command": command, "shell": shell, "timeout_seconds": timeout},
            timeout=float(timeout + self.connect_timeout),
        )
        if resp.get("timed_out"):
            raise OperationTimeoutError(f"vsock run: {command}", timeout)
        if not resp.get("ok"):
            raise SmolVMError(f"guest agent error during run: {resp.get('error', resp)}")
        return CommandResult(
            exit_code=int(resp.get("exit_code", -1)),
            stdout=str(resp.get("stdout", "")),
            stderr=str(resp.get("stderr", "")),
        )

    def put_file(self, local_path: str | Path, remote_path: str) -> None:
        if not remote_path:
            raise ValueError("remote_path cannot be empty")
        source = Path(local_path)
        if not source.exists():
            raise ValueError(f"local_path does not exist: {source}")
        if not source.is_file():
            raise ValueError(f"local_path is not a file: {source}")
        mode = stat.S_IMODE(source.stat().st_mode)
        payload = {
            "path": remote_path,
            "name": source.name,
            "mode": mode,
            "data_base64": base64.b64encode(source.read_bytes()).decode("ascii"),
        }
        resp = self._request_json("POST", "/files/put", payload)
        if not resp.get("ok"):
            raise SmolVMError(
                f"Failed to upload file to guest '{remote_path}': {resp.get('error')}"
            )

    def get_file(self, remote_path: str, local_path: str | Path) -> Path:
        if not remote_path:
            raise ValueError("remote_path cannot be empty")
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        query = urllib.parse.urlencode({"path": remote_path})
        resp = self._request_json("GET", f"/files/get?{query}")
        if not resp.get("ok"):
            raise SmolVMError(f"Failed to download guest file '{remote_path}': {resp.get('error')}")
        data_b64 = resp.get("data_base64")
        if not isinstance(data_b64, str):
            raise SmolVMError(f"Guest file response for '{remote_path}' did not include data")
        data = base64.b64decode(data_b64.encode("ascii"))
        destination.write_bytes(data)
        size = resp.get("size")
        if size is not None and int(size) != len(data):
            raise SmolVMError(
                f"Guest file response for '{remote_path}' had size {size}, got {len(data)} bytes"
            )
        mode = resp.get("mode")
        if mode is not None:
            os.chmod(destination, int(mode))
        return destination

    def wait_ready(self, timeout: float = 60.0, interval: float = 0.1) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        if interval <= 0:
            raise ValueError("interval must be > 0")
        started_at = time.monotonic()
        deadline = time.monotonic() + timeout
        last_error = ""
        target = self.uds_path if self.uds_path is not None else f"cid={self.guest_cid}"
        logger.info("Waiting for Rust guest agent on vsock %s (timeout=%.0fs)", target, timeout)

        while time.monotonic() < deadline:
            elapsed = time.monotonic() - started_at
            poll_interval = interval
            if elapsed < _READY_FAST_POLL_WINDOW:
                poll_interval = min(interval, _READY_FAST_POLL_INTERVAL)
            try:
                resp = self._request_json("GET", "/health", timeout=max(1.0, poll_interval))
                if resp.get("status") == "ok":
                    self._ready = True
                    logger.info("Rust guest agent is ready on vsock %s", target)
                    return
                last_error = str(resp)
            except (OSError, SmolVMError, ValueError, OperationTimeoutError) as exc:
                last_error = str(exc)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))

        raise OperationTimeoutError(
            f"wait_ready(rust-vsock {target}): last error: {last_error}", timeout
        )

    def close(self) -> None:
        self._ready = False

    @property
    def connected(self) -> bool:
        return self._ready
