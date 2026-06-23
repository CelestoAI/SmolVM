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

import io
import json
import os
import queue
import socket
import tarfile
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from smolvm.comm.base import CommChannel
from smolvm.comm.rust_http_vsock_channel import (
    SMOLVM_TERMINAL_PORT,
    ControlCapabilities,
    RustHttpVsockChannel,
    _directory_to_tar,
    _pack_terminal_frame,
    _read_terminal_frame,
    _SocketHTTPConnection,
)
from smolvm.exceptions import OperationTimeoutError, SmolVMError

Handler = Callable[[str, str, bytes], Any]


def test_control_capabilities_accept_flat_dotted_features() -> None:
    capabilities = ControlCapabilities(
        protocol_version=2,
        features={"files.stream": True, "ports": {"wait": "true"}},
        limits={},
    )

    assert capabilities.enabled("file_raw", "files.stream")
    assert capabilities.enabled("ports.wait")
    assert not capabilities.enabled("env.managed")


def test_terminal_frame_helpers_round_trip_payload() -> None:
    host, guest = socket.socketpair()
    try:
        host.sendall(_pack_terminal_frame(101, b"hello"))
        assert _read_terminal_frame(guest) == (101, b"hello")
    finally:
        host.close()
        guest.close()


def test_feature_required_error_names_recreate_commands() -> None:
    channel = RustHttpVsockChannel.from_cid(42, sandbox_name="sbx-riemann")
    channel._capabilities = ControlCapabilities(
        protocol_version=2,
        features={},
        limits={},
    )

    with pytest.raises(SmolVMError) as exc_info:
        channel.attach_terminal()

    message = str(exc_info.value)
    assert "Sandbox sbx-riemann was created from an older image" in message
    assert "smolvm sandbox delete sbx-riemann" in message
    assert "smolvm sandbox create --name sbx-riemann" in message


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
                result = handler(method, path, body)
                status = 200
                headers: dict[str, str] = {}
                if isinstance(result, tuple):
                    if len(result) == 3:
                        status, result, headers = result
                    else:
                        status, result = result
                if isinstance(result, bytes):
                    payload = result
                    headers.setdefault("Content-Type", "application/octet-stream")
                else:
                    payload = (
                        result.encode() if isinstance(result, str) else json.dumps(result).encode()
                    )
                    headers.setdefault("Content-Type", "application/json")
                status_text = "OK" if status < 400 else "ERROR"
                header_bytes = b"".join(
                    f"{name}: {value}\r\n".encode() for name, value in headers.items()
                )
                guest.sendall(
                    f"HTTP/1.1 {status} {status_text}\r\n".encode()
                    + header_bytes
                    + f"Content-Length: {len(payload)}\r\n".encode()
                    + b"Connection: close\r\n\r\n"
                    + payload
                )

        threading.Thread(target=_serve, daemon=True).start()
        return host


class FakeTerminalChannel(FakeRustChannel):
    def __init__(self, handlers: list[Handler]) -> None:
        super().__init__(handlers)
        self.terminal_requests: list[dict[str, Any]] = []
        self.terminal_stdin: list[bytes] = []

    def _open_port(self, port: int) -> socket.socket:
        if port != SMOLVM_TERMINAL_PORT:
            return self._open()

        host, guest = socket.socketpair()

        def _serve() -> None:
            with guest:
                line = b""
                while not line.endswith(b"\n"):
                    line += guest.recv(1)
                self.terminal_requests.append(json.loads(line))
                guest.sendall(json.dumps({"ok": True, "pid": 1234}).encode() + b"\n")
                frame_type, payload = _read_terminal_frame(guest)
                assert frame_type == 1
                self.terminal_stdin.append(payload)
                guest.sendall(_pack_terminal_frame(101, b"ok\r\n"))
                guest.sendall(_pack_terminal_frame(102, json.dumps({"exit_code": 7}).encode()))

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


def test_from_uds_closes_socket_when_connect_rejected() -> None:
    sock_dir = tempfile.TemporaryDirectory(prefix="svsock-", dir="/tmp")
    uds = str(Path(sock_dir.name) / "vsock.sock")
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
        sock_dir.cleanup()


def test_socket_http_connection_uses_request_timeout_for_reads() -> None:
    host, guest = socket.socketpair()
    host.settimeout(1.0)
    try:
        conn = _SocketHTTPConnection(lambda: host, timeout=123.0)
        conn.connect()
        assert conn.sock is not None
        assert conn.sock.gettimeout() == 123.0
    finally:
        host.close()
        guest.close()


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


def test_attach_terminal_streams_stdin_stdout_and_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"exit\n")
    os.close(write_fd)

    class _PipeInput:
        buffer: _PipeInput

        def __init__(self, fd: int) -> None:
            self.fd = fd
            self.buffer = self

        def fileno(self) -> int:
            return self.fd

    output = io.BytesIO()
    monkeypatch.setattr("smolvm.comm.rust_http_vsock_channel.sys.stdin", _PipeInput(read_fd))
    monkeypatch.setattr("smolvm.comm.rust_http_vsock_channel.sys.stdout", output)
    monkeypatch.setenv("TERM", "xterm-test")

    channel = FakeTerminalChannel([_capabilities({"terminal": True})])
    try:
        exit_code = channel.attach_terminal()
    finally:
        os.close(read_fd)

    assert exit_code == 7
    assert output.getvalue() == b"ok\r\n"
    assert channel.terminal_stdin == [b"exit\n"]
    assert channel.terminal_requests[0]["version"] == 1
    assert channel.terminal_requests[0]["term"] == "xterm-test"


def test_attach_terminal_requires_terminal_capability() -> None:
    channel = FakeRustChannel([_capabilities({})])

    with pytest.raises(SmolVMError, match="fast shell access"):
        channel.attach_terminal()


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


def test_sync_posts_dedicated_endpoint() -> None:
    def _handler(method: str, path: str, body: bytes) -> dict:
        assert method == "POST"
        assert path == "/sync"
        assert body == b""
        return {"ok": True}

    FakeRustChannel([_capabilities({"sync": True}), _handler]).sync(timeout=10)


def test_sync_error_maps_to_smolvm_error() -> None:
    channel = FakeRustChannel(
        [_capabilities({"sync": True}), lambda method, path, body: {"ok": False, "error": "busy"}]
    )

    with pytest.raises(SmolVMError, match="busy"):
        channel.sync()


def test_sync_requires_sync_capability() -> None:
    channel = FakeRustChannel([_capabilities({})])

    with pytest.raises(SmolVMError, match="saving files before shutdown"):
        channel.sync(timeout=10)


def test_put_file_requires_streaming_capability(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("payload")

    channel = FakeRustChannel([_capabilities({})])
    with pytest.raises(SmolVMError, match="fast file transfer"):
        channel.put_file(source, "/tmp/source.txt")


def test_put_and_get_file_use_raw_streaming_and_cache_capabilities(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("payload")
    source.chmod(0o644)
    destination = tmp_path / "download.txt"

    def _raw_put(method: str, path: str, body: bytes) -> dict:
        assert method == "PUT"
        assert path == "/files/content?path=%2Ftmp%2Fsource.txt&name=source.txt&mode=420"
        assert body == b"payload"
        return {"ok": True}

    def _raw_get(method: str, path: str, body: bytes) -> tuple[int, bytes, dict[str, str]]:
        assert method == "GET"
        assert path == "/files/content?path=%2Ftmp%2Fsource.txt"
        assert body == b""
        return (
            200,
            b"payload",
            {"x-smolvm-file-mode": "640", "x-smolvm-file-size": "7"},
        )

    channel = FakeRustChannel([_capabilities({"file_raw": True}), _raw_put, _raw_get])
    channel.put_file(source, "/tmp/source.txt")
    channel.get_file("/tmp/source.txt", destination)

    assert destination.read_text() == "payload"
    assert destination.stat().st_mode & 0o777 == 0o640
    assert [request[:2] for request in channel.requests] == [
        ("GET", "/capabilities"),
        ("PUT", "/files/content?path=%2Ftmp%2Fsource.txt&name=source.txt&mode=420"),
        ("GET", "/files/content?path=%2Ftmp%2Fsource.txt"),
    ]


def test_put_file_does_not_fallback_when_raw_endpoint_missing(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("payload")

    def _missing_raw(method: str, path: str, body: bytes) -> tuple[int, str]:
        assert method == "PUT"
        assert path.startswith("/files/content?")
        return (404, "")

    channel = FakeRustChannel([_capabilities({"file_raw": True}), _missing_raw])
    with pytest.raises(SmolVMError, match="guest agent HTTP 404"):
        channel.put_file(source, "/tmp/source.txt")


def test_put_file_rejects_local_size_over_cap_before_upload(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("payload")

    def _unexpected_put(method: str, path: str, body: bytes) -> dict:
        raise AssertionError(f"unexpected request: {method} {path} {len(body)} bytes")

    channel = FakeRustChannel(
        [
            _capabilities(
                {"file_raw": True},
                limits={"max_stream_size_bytes": 4},
            ),
            _unexpected_put,
        ]
    )

    with pytest.raises(SmolVMError, match="up to 4 bytes"):
        channel.put_file(source, "/tmp/source.txt")
    assert [request[:2] for request in channel.requests] == [("GET", "/capabilities")]


def test_get_directory_rejects_unsafe_tar_entries(tmp_path: Path) -> None:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo("../escape.txt")
        payload = b"nope"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    def _tar_get(method: str, path: str, body: bytes) -> tuple[int, bytes, dict[str, str]]:
        assert method == "GET"
        assert path == "/directories/tar?path=%2Ftmp%2Fdata"
        return (200, archive.getvalue(), {"Content-Type": "application/x-tar"})

    channel = FakeRustChannel([_capabilities({"dir_tar": True}), _tar_get])
    with pytest.raises(SmolVMError, match="outside destination"):
        channel.get_directory("/tmp/data", tmp_path / "download")
    assert not (tmp_path / "escape.txt").exists()


def test_raw_file_download_rejects_declared_size_over_cap(tmp_path: Path) -> None:
    destination = tmp_path / "download.txt"

    def _raw_get(method: str, path: str, body: bytes) -> tuple[int, bytes, dict[str, str]]:
        assert method == "GET"
        assert path == "/files/content?path=%2Ftmp%2Fsource.txt"
        assert body == b""
        return (200, b"payload", {"x-smolvm-file-size": "7"})

    channel = FakeRustChannel(
        [
            _capabilities(
                {"file_raw": True},
                limits={"max_stream_size_bytes": 4},
            ),
            _raw_get,
        ]
    )

    with pytest.raises(SmolVMError, match="exceeded 4 bytes"):
        channel.get_file("/tmp/source.txt", destination)


def test_directory_transfer_requires_tar_capability(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.txt").write_text("hello")
    channel = FakeRustChannel([_capabilities({})])

    with pytest.raises(SmolVMError, match="directory transfer"):
        channel.put_directory(source, "/tmp/target")


def test_directory_tar_strips_owner_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    private_key = source / "id_ed25519"
    private_key.write_text("PRIVATE")
    private_key.chmod(0o600)

    data = _directory_to_tar(source)

    assert b"././@PaxHeader" not in data
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        members = archive.getmembers()

    assert members
    assert all(member.uid == 0 and member.gid == 0 for member in members)
    assert all(member.uname == "" and member.gname == "" for member in members)
    key_member = next(member for member in members if member.name == "id_ed25519")
    assert key_member.mode & 0o777 == 0o600


def test_directory_tar_wraps_ustar_path_limit_errors(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    long_name = "a" * 120
    (source / long_name).write_text("too long for ustar")

    with pytest.raises(SmolVMError, match="file path is too long"):
        _directory_to_tar(source)


def test_env_helpers_use_v2_env_endpoint() -> None:
    def _set(method: str, path: str, body: bytes) -> dict:
        assert method == "PUT"
        assert path == "/env"
        request = json.loads(body)
        assert request == {"vars": {"FOO": "bar"}, "merge": True}
        return {"ok": True, "vars": {"FOO": "bar"}}

    def _get(method: str, path: str, body: bytes) -> dict:
        assert method == "GET"
        assert path == "/env"
        return {"ok": True, "vars": {"FOO": "bar"}}

    def _delete(method: str, path: str, body: bytes) -> dict:
        assert method == "DELETE"
        assert path == "/env"
        assert json.loads(body) == {"keys": ["FOO"]}
        return {"ok": True, "vars": {}}

    channel = FakeRustChannel([_capabilities({"env_managed": True}), _set, _get, _get, _delete])
    assert channel.set_env_vars({"FOO": "bar"}) == ["FOO"]
    assert channel.list_env_vars() == {"FOO": "bar"}
    assert channel.unset_env_vars(["FOO"]) == {"FOO": "bar"}


def test_env_helpers_require_managed_env_capability() -> None:
    channel = FakeRustChannel([_capabilities({})])

    with pytest.raises(SmolVMError, match="managed environment variables"):
        channel.set_managed_env({"FOO": "bar"})


def test_wait_for_ports_and_boot_milestones_use_v2_endpoints() -> None:
    def _ports(method: str, path: str, body: bytes) -> dict:
        assert method == "POST"
        assert path == "/ports/wait"
        request = json.loads(body)
        assert request == {"ports": [3000], "timeout_ms": 1000, "host": "127.0.0.1"}
        return {"ok": True, "ready_ports": [3000]}

    def _milestones(method: str, path: str, body: bytes) -> dict:
        assert method == "GET"
        assert path == "/boot/milestones"
        return {
            "ok": True,
            "milestones": [{"stage": "guest-agent-started", "uptime_s": 0.24}],
        }

    channel = FakeRustChannel([_ports, _milestones])
    assert channel.wait_for_ports([3000], timeout=1.0) == [3000]
    assert channel.boot_milestones() == [{"stage": "guest-agent-started", "uptime_s": 0.24}]


def test_wait_for_ports_validates_inputs_before_request() -> None:
    channel = FakeRustChannel([])

    with pytest.raises(ValueError, match="ports cannot be empty"):
        channel.wait_for_ports([])
    with pytest.raises(ValueError, match="valid IP address"):
        channel.wait_for_ports([3000], host="localhost")


def test_wait_for_ports_preserves_guest_validation_errors() -> None:
    def _ports(method: str, path: str, body: bytes) -> dict:
        assert method == "POST"
        assert path == "/ports/wait"
        return {"ok": False, "error": "invalid host: not-an-ip"}

    channel = FakeRustChannel([_ports])

    with pytest.raises(SmolVMError, match="invalid host"):
        channel.wait_for_ports([3000])


def test_wait_for_ports_preserves_guest_timeout_validation_errors() -> None:
    def _ports(method: str, path: str, body: bytes) -> dict:
        assert method == "POST"
        assert path == "/ports/wait"
        return {"ok": False, "error": "timeout_ms must be at most 300000"}

    channel = FakeRustChannel([_ports])

    with pytest.raises(SmolVMError, match="timeout_ms must be at most"):
        channel.wait_for_ports([3000])


def _capabilities(features: dict[str, bool], *, limits: dict[str, Any] | None = None) -> Handler:
    def _handler(method: str, path: str, body: bytes) -> dict:
        assert method == "GET"
        assert path == "/capabilities"
        return {"protocol_version": 2, "features": features, "limits": limits or {}}

    return _handler
