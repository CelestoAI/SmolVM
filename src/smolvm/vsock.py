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

"""Host-side vsock client for guest control-plane requests."""

from __future__ import annotations

import json
import socket
from typing import Any

from smolvm.exceptions import SmolVMError
from smolvm.types import CommandResult


class VsockClient:
    """Simple request/response client over AF_VSOCK."""

    def __init__(self, guest_cid: int, guest_port: int, *, timeout: float = 30.0) -> None:
        self.guest_cid = guest_cid
        self.guest_port = guest_port
        self.timeout = timeout

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not hasattr(socket, "AF_VSOCK"):
            raise SmolVMError("AF_VSOCK is not supported on this platform")

        raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")

        try:
            with socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect((self.guest_cid, self.guest_port))
                sock.sendall(raw)
                response = self._recv_line(sock)
        except OSError as exc:
            raise SmolVMError(f"Vsock transport error: {exc}") from exc

        try:
            data = json.loads(response)
        except json.JSONDecodeError as exc:
            raise SmolVMError("Invalid JSON response from vsock agent") from exc

        if not isinstance(data, dict):
            raise SmolVMError("Invalid vsock response shape")
        if data.get("ok") is False:
            raise SmolVMError(data.get("error", "vsock operation failed"))
        return data

    @staticmethod
    def _recv_line(sock: socket.socket) -> str:
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break

        payload = b"".join(chunks).split(b"\n", 1)[0]
        return payload.decode("utf-8")

    def health(self) -> dict[str, Any]:
        return self._request({"op": "health"})

    def run(
        self,
        command: str,
        *,
        timeout: int = 30,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        token: str | None = None,
    ) -> CommandResult:
        response = self._request(
            {
                "op": "exec",
                "command": command,
                "timeout": timeout,
                "env": env or {},
                "cwd": cwd,
                "token": token,
            }
        )

        result = response.get("result", response)
        return CommandResult(
            exit_code=int(result.get("exit_code", 1)),
            stdout=str(result.get("stdout", "")),
            stderr=str(result.get("stderr", "")),
        )
