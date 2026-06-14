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

"""Tests for the Rust HTTP-over-vsock host channel."""

from __future__ import annotations

import base64
import json
import queue
import socket
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from smolvm.comm.base import CommChannel
from smolvm.comm.rust_http_vsock_channel import RustHttpVsockChannel
from smolvm.exceptions import OperationTimeoutError, SmolVMError

Handler = Callable[[str, str, bytes], dict]


class FakeRustChannel(RustHttpVsockChannel):
    def __init__(self, handlers: list[Handler]) -> None:
        super().__init__(guest_cid=42)
        self.handlers = handlers
        self.requests: list[tuple[str, str, bytes]] = []

    def _open(self) -> socket.socket:
        host, guest = socket.socketpair()
        handler = self.handlers.pop(0)

        def _serve() -> None:
            with guest:
                data = _read_http_request(guest)
                method, path, body = data
                self.requests.append(data)
                payload = json.dumps(handler(method, path, body)).encode()
                guest.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(payload)}\r\n".encode()
                    + b"Connection: close\r\n\r\n"
                    + payload
                )

        threading.Thread(target=_serve, daemon=True).start()
        return host


def _read_http_request(sock: socket.socket) -> tuple[str, str, bytes]:
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError("HTTP request closed before headers")
        buf.extend(chunk)
    header_bytes, body = bytes(buf).split(b"\r\n\r\n", 1)
    lines = header_bytes.decode().split("\r\n")
    method, path, _version = lines[0].split(" ", 2)
    content_length = 0
    for line in lines[1:]:
        name, _sep, value = line.partition(":")
        if name.lower() == "content-length":
            content_length = int(value.strip())
    while len(body) < content_length:
        chunk = sock.recv(content_length - len(body))
        if not chunk:
            raise AssertionError("HTTP request closed before full body")
        body += chunk
    return method, path, body


def test_rust_http_channel_satisfies_comm_channel() -> None:
    channel = RustHttpVsockChannel.from_cid(42)
    assert isinstance(channel, CommChannel)
    assert channel.kind == "vsock"


def test_wait_ready_uses_health() -> None:
    channel = FakeRustChannel(
        [lambda method, path, body: {"status": "ok", "agent_version": "0.1.0"}]
    )
    channel.wait_ready(timeout=1)
    assert channel.connected is True
    assert channel.requests[0][0:2] == ("GET", "/health")


def test_wait_ready_polls_quickly_during_early_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = FakeRustChannel(
        [
            lambda method, path, body: {"status": "starting"},
            lambda method, path, body: {"status": "starting"},
            lambda method, path, body: {"status": "ok", "agent_version": "0.1.0"},
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(
        "smolvm.comm.rust_http_vsock_channel.time.sleep",
        lambda duration: sleeps.append(duration),
    )

    channel.wait_ready(timeout=1, interval=0.1)

    assert channel.connected is True
    assert sleeps == [0.02, 0.02]


def test_from_uds_closes_socket_when_connect_rejected(tmp_path: Path) -> None:
    uds = str(tmp_path / "vsock.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(uds)
    server.listen(1)
    closed = queue.Queue()

    def _serve() -> None:
        conn, _ = server.accept()
        with conn:
            line = b""
            while not line.endswith(b"\n"):
                chunk = conn.recv(1)
                if not chunk:
                    break
                line += chunk
            assert line.startswith(b"CONNECT")
            conn.sendall(b"ERR denied\n")
            conn.settimeout(2)
            closed.put(conn.recv(1))

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        channel = RustHttpVsockChannel.from_uds(uds)
        with pytest.raises(SmolVMError, match="CONNECT handshake failed"):
            channel._open_uds()
        assert closed.get(timeout=2) == b""
    finally:
        server.close()
        thread.join(timeout=5)


def test_run_maps_command_result() -> None:
    def _handler(method: str, path: str, body: bytes) -> dict:
        assert method == "POST"
        assert path == "/exec"
        request = json.loads(body)
        assert request["command"] == "printf ok"
        assert request["shell"] == "raw"
        return {"ok": True, "exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False}

    result = FakeRustChannel([_handler]).run("printf ok", shell="raw")
    assert result.exit_code == 0
    assert result.stdout == "ok"


def test_run_serializes_float_timeout_as_integer_seconds() -> None:
    def _handler(expected_timeout_seconds: int):
        def handle(method: str, path: str, body: bytes) -> dict:
            assert method == "POST"
            assert path == "/exec"
            request = json.loads(body)
            assert request["timeout_seconds"] == expected_timeout_seconds
            assert isinstance(request["timeout_seconds"], int)
            return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

        return handle

    result = FakeRustChannel([_handler(10)]).run("true", timeout=10.0, shell="raw")
    assert result.exit_code == 0

    result = FakeRustChannel([_handler(11)]).run("true", timeout=10.1, shell="raw")
    assert result.exit_code == 0


def test_run_timeout_maps_to_operation_timeout() -> None:
    channel = FakeRustChannel(
        [
            lambda method, path, body: {
                "ok": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": "",
                "timed_out": True,
                "error": "Command timed out after 1s",
            }
        ]
    )
    with pytest.raises(OperationTimeoutError):
        channel.run("sleep 10", timeout=1)


def test_put_and_get_file(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("payload")
    destination = tmp_path / "download.txt"

    def _put(method: str, path: str, body: bytes) -> dict:
        request = json.loads(body)
        assert method == "POST"
        assert path == "/files/put"
        assert request["path"] == "/tmp/source.txt"
        assert base64.b64decode(request["data_base64"]) == b"payload"
        return {"ok": True}

    def _get(method: str, path: str, body: bytes) -> dict:
        assert method == "GET"
        assert path == "/files/get?path=%2Ftmp%2Fsource.txt"
        return {
            "ok": True,
            "mode": 0o640,
            "size": 7,
            "data_base64": base64.b64encode(b"payload").decode(),
        }

    channel = FakeRustChannel([_put, _get])
    channel.put_file(source, "/tmp/source.txt")
    channel.get_file("/tmp/source.txt", destination)
    assert destination.read_text() == "payload"
    assert destination.stat().st_mode & 0o777 == 0o640
