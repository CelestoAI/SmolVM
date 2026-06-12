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

"""Host-side vsock transport — talks to the guest agent over ``AF_VSOCK``.

:class:`VsockChannel` implements :class:`~smolvm.comm.base.CommChannel` by
speaking :mod:`smolvm.comm.protocol` to the agent baked into the guest image.
It opens a fresh connection per request (one request per connection — vsock
connect is cheap and the agent is unauthenticated, so there is no handshake to
amortize), which keeps the stream impossible to desync.

Two ways to reach the guest:

- :meth:`from_cid` — host ``AF_VSOCK`` connect to ``(guest_cid, port)``. This is
  the native QEMU ``vhost-vsock-pci`` path and is **Linux-only** (the host
  needs ``/dev/vhost-vsock``).
- :meth:`from_uds` — connect to a host Unix socket and issue the Firecracker
  ``CONNECT <port>`` handshake. Used by Firecracker and (later) the macOS
  libkrun proxy; works cross-platform.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import stat
import time
from pathlib import Path

from smolvm.comm import protocol
from smolvm.comm.base import CommChannelKind, ShellMode
from smolvm.exceptions import OperationTimeoutError, SmolVMError
from smolvm.types import CommandResult

logger = logging.getLogger(__name__)


class VsockChannel:
    """Drive a guest over its vsock control agent.

    Construct via :meth:`from_cid` or :meth:`from_uds`; the direct constructor
    takes exactly one of *guest_cid* or *uds_path*.
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
    ) -> VsockChannel:
        """Reach the guest via host ``AF_VSOCK`` (native QEMU on Linux)."""
        return cls(guest_cid=guest_cid, agent_port=agent_port, connect_timeout=connect_timeout)

    @classmethod
    def from_uds(
        cls,
        uds_path: str | Path,
        *,
        agent_port: int = protocol.SMOLVM_AGENT_PORT,
        connect_timeout: int = 10,
    ) -> VsockChannel:
        """Reach the guest via a host Unix socket (Firecracker / libkrun)."""
        return cls(uds_path=uds_path, agent_port=agent_port, connect_timeout=connect_timeout)

    # ── Connection ──────────────────────────────────────────────

    def _open(self) -> socket.socket:
        """Open a connection to the guest agent, ready for framed I/O."""
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
            # Firecracker-style host->guest handshake: ask to be connected to
            # the agent's vsock port, then expect an "OK <n>" acknowledgement.
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
        """Read a single ``\\n``-terminated line (for the CONNECT handshake)."""
        buf = bytearray()
        while not buf.endswith(b"\n"):
            byte = sock.recv(1)
            if not byte:
                break
            buf.extend(byte)
        return buf.decode("utf-8", errors="replace").strip()

    # ── CommChannel interface ───────────────────────────────────

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

        stdout = bytearray()
        stderr = bytearray()
        sock = self._open()
        try:
            # Read deadline a little beyond the command timeout, so the agent's
            # own timeout fires first and we still receive its report.
            sock.settimeout(float(timeout) + float(self.connect_timeout))
            protocol.send_json(
                sock,
                {
                    "v": protocol.PROTOCOL_VERSION,
                    "op": "run",
                    "cmd": command,
                    "shell": shell,
                    "timeout": timeout,
                },
            )
            while True:
                frame_type, payload = protocol.recv_frame(sock)
                if frame_type == protocol.FRAME_STDOUT:
                    stdout.extend(payload)
                elif frame_type == protocol.FRAME_STDERR:
                    stderr.extend(payload)
                elif frame_type == protocol.FRAME_JSON:
                    msg = json.loads(payload.decode("utf-8"))
                    return self._finish_run(command, timeout, msg, stdout, stderr)
                else:  # pragma: no cover - defensive
                    raise SmolVMError(f"unexpected frame type {frame_type} during run")
        except TimeoutError as exc:
            raise OperationTimeoutError(f"vsock run: {command}", timeout) from exc
        finally:
            sock.close()

    def _finish_run(
        self,
        command: str,
        timeout: int,
        msg: dict,
        stdout: bytearray,
        stderr: bytearray,
    ) -> CommandResult:
        op = msg.get("op")
        if op == "exit":
            return CommandResult(
                exit_code=int(msg["exit_code"]),
                stdout=bytes(stdout).decode("utf-8", errors="replace"),
                stderr=bytes(stderr).decode("utf-8", errors="replace"),
            )
        if op == "timeout":
            raise OperationTimeoutError(f"vsock run: {command}", timeout)
        raise SmolVMError(f"guest agent error during run: {msg.get('error', msg)}")

    def put_file(self, local_path: str | Path, remote_path: str) -> None:
        if not remote_path:
            raise ValueError("remote_path cannot be empty")
        source = Path(local_path)
        if not source.exists():
            raise ValueError(f"local_path does not exist: {source}")

        data = source.read_bytes()
        mode = stat.S_IMODE(source.stat().st_mode)
        sock = self._open()
        try:
            protocol.send_json(
                sock,
                {
                    "v": protocol.PROTOCOL_VERSION,
                    "op": "put_file",
                    "path": remote_path,
                    "name": source.name,
                    "mode": mode,
                    "size": len(data),
                },
            )
            for start in range(0, len(data), protocol.CHUNK_SIZE):
                chunk = data[start : start + protocol.CHUNK_SIZE]
                protocol.send_frame(sock, protocol.FRAME_DATA, chunk)
            protocol.send_frame(sock, protocol.FRAME_EOF)
            resp = protocol.recv_json(sock)
            if not resp.get("ok"):
                raise SmolVMError(
                    f"Failed to upload file to guest '{remote_path}': {resp.get('error')}"
                )
        finally:
            sock.close()

    def get_file(self, remote_path: str, local_path: str | Path) -> Path:
        if not remote_path:
            raise ValueError("remote_path cannot be empty")
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)

        sock = self._open()
        try:
            protocol.send_json(
                sock,
                {"v": protocol.PROTOCOL_VERSION, "op": "get_file", "path": remote_path},
            )
            header = protocol.recv_json(sock)
            if not header.get("ok"):
                raise SmolVMError(
                    f"Failed to download guest file '{remote_path}': {header.get('error')}"
                )
            mode = header.get("mode")
            with open(destination, "wb") as handle:  # noqa: SIM115 - within try for cleanup
                while True:
                    frame_type, payload = protocol.recv_frame(sock)
                    if frame_type == protocol.FRAME_EOF:
                        break
                    if frame_type != protocol.FRAME_DATA:  # pragma: no cover - defensive
                        raise SmolVMError(f"unexpected frame type {frame_type} during download")
                    handle.write(payload)
            if mode is not None:
                os.chmod(destination, int(mode))
        finally:
            sock.close()
        return destination

    def wait_ready(self, timeout: float = 60.0, interval: float = 0.1) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        deadline = time.monotonic() + timeout
        last_error = ""
        target = self.uds_path if self.uds_path is not None else f"cid={self.guest_cid}"
        logger.info("Waiting for guest agent on vsock %s (timeout=%.0fs)", target, timeout)

        while time.monotonic() < deadline:
            try:
                sock = self._open()
                try:
                    protocol.send_json(sock, {"v": protocol.PROTOCOL_VERSION, "op": "ping"})
                    resp = protocol.recv_json(sock)
                finally:
                    sock.close()
                if resp.get("ok"):
                    self._ready = True
                    logger.info("Guest agent is ready on vsock %s", target)
                    return
                last_error = str(resp.get("error", resp))
            except (OSError, SmolVMError, ValueError) as exc:
                last_error = str(exc)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))

        raise OperationTimeoutError(
            f"wait_ready(vsock {target}): last error: {last_error}", timeout
        )

    def close(self) -> None:
        """No persistent connection is held; present for interface parity."""
        self._ready = False

    @property
    def connected(self) -> bool:
        """Whether the agent has answered at least once since the last reset."""
        return self._ready
