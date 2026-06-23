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
import io
import json
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
    ControlCapabilities,
    RustHttpVsockChannel,
    _directory_to_tar,
)
from smolvm.exceptions import OperationTimeoutError, SmolVMError
from smolvm.types import CommandResult

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


def test_sync_posts_dedicated_endpoint() -> None:
    def _handler(method: str, path: str, body: bytes) -> dict:
        assert method == "POST"
        assert path == "/sync"
        assert body == b""
        return {"ok": True}

    FakeRustChannel([_handler]).sync(timeout=10)


def test_sync_error_maps_to_smolvm_error() -> None:
    channel = FakeRustChannel([lambda method, path, body: {"ok": False, "error": "busy"}])

    with pytest.raises(SmolVMError, match="busy"):
        channel.sync()


def test_sync_falls_back_to_raw_exec_when_endpoint_missing() -> None:
    def _missing_sync(method: str, path: str, body: bytes) -> tuple[int, str]:
        assert method == "POST"
        assert path == "/sync"
        assert body == b""
        return (404, "")

    def _legacy_exec(method: str, path: str, body: bytes) -> dict:
        assert method == "POST"
        assert path == "/exec"
        request = json.loads(body)
        assert request["command"] == "sync"
        assert request["shell"] == "raw"
        assert request["timeout_seconds"] == 10
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

    channel = FakeRustChannel([_missing_sync, _legacy_exec])
    channel.sync(timeout=10)


def test_sync_fallback_exec_failure_maps_to_smolvm_error() -> None:
    def _missing_sync(method: str, path: str, body: bytes) -> tuple[int, str]:
        return (404, "")

    def _legacy_exec(method: str, path: str, body: bytes) -> dict:
        return {"ok": True, "exit_code": 1, "stdout": "", "stderr": "nope", "timed_out": False}

    channel = FakeRustChannel([_missing_sync, _legacy_exec])
    with pytest.raises(SmolVMError, match="legacy sync fallback"):
        channel.sync(timeout=10)


def test_put_and_get_file(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("payload")
    source.chmod(0o644)
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

    channel = FakeRustChannel([_capabilities({"file_base64": True}), _put, _get])
    channel.put_file(source, "/tmp/source.txt")
    channel.get_file("/tmp/source.txt", destination)
    assert destination.read_text() == "payload"
    assert destination.stat().st_mode & 0o777 == 0o640


def test_put_and_get_file_prefer_raw_streaming_and_cache_capabilities(tmp_path: Path) -> None:
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

    channel = FakeRustChannel(
        [_capabilities({"file_base64": True, "file_raw": True}), _raw_put, _raw_get]
    )
    channel.put_file(source, "/tmp/source.txt")
    channel.get_file("/tmp/source.txt", destination)

    assert destination.read_text() == "payload"
    assert destination.stat().st_mode & 0o777 == 0o640
    assert [request[:2] for request in channel.requests] == [
        ("GET", "/capabilities"),
        ("PUT", "/files/content?path=%2Ftmp%2Fsource.txt&name=source.txt&mode=420"),
        ("GET", "/files/content?path=%2Ftmp%2Fsource.txt"),
    ]


def test_put_file_falls_back_to_base64_when_raw_endpoint_missing(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("payload")

    def _missing_raw(method: str, path: str, body: bytes) -> tuple[int, str]:
        assert method == "PUT"
        assert path.startswith("/files/content?")
        return (404, "")

    def _base64_put(method: str, path: str, body: bytes) -> dict:
        request = json.loads(body)
        assert method == "POST"
        assert path == "/files/put"
        assert base64.b64decode(request["data_base64"]) == b"payload"
        return {"ok": True}

    channel = FakeRustChannel(
        [_capabilities({"file_base64": True, "file_raw": True}), _missing_raw, _base64_put]
    )
    channel.put_file(source, "/tmp/source.txt")


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


def test_legacy_directory_fallback_uses_guest_mktemp(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.txt").write_text("hello")
    commands: list[str] = []
    uploads: list[str] = []

    channel = RustHttpVsockChannel(guest_cid=42)

    def fake_run(command: str, timeout: float = 30, shell: str = "login") -> CommandResult:
        commands.append(command)
        if command.startswith("mktemp "):
            return CommandResult(exit_code=0, stdout="/tmp/smolvm-dir.ABC123.tar\n", stderr="")
        return CommandResult(exit_code=0, stdout="", stderr="")

    def fake_put_file(local_path: str | Path, remote_path: str) -> None:
        uploads.append(remote_path)

    channel.run = fake_run  # type: ignore[method-assign]
    channel.put_file = fake_put_file  # type: ignore[method-assign]

    channel._put_directory_legacy(source, "/tmp/target")

    assert uploads == ["/tmp/smolvm-dir.ABC123.tar"]
    assert commands[0].startswith("mktemp /tmp/smolvm-dir.")
    assert "trap" in commands[1]
    assert 'tar -xf "$remote_tmp"' in commands[1]
    assert commands[-1] == "rm -f -- /tmp/smolvm-dir.ABC123.tar"


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


def test_legacy_directory_download_uses_guest_mktemp(tmp_path: Path) -> None:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo("note.txt")
        payload = b"hello"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    commands: list[str] = []
    downloads: list[str] = []
    channel = RustHttpVsockChannel(guest_cid=42)

    def fake_run(command: str, timeout: float = 30, shell: str = "login") -> CommandResult:
        commands.append(command)
        if command.startswith("mktemp "):
            return CommandResult(exit_code=0, stdout="/tmp/smolvm-dir.DEF456.tar\n", stderr="")
        return CommandResult(exit_code=0, stdout="", stderr="")

    def fake_get_file(remote_path: str, local_path: str | Path) -> Path:
        downloads.append(remote_path)
        destination = Path(local_path)
        destination.write_bytes(archive.getvalue())
        return destination

    channel.run = fake_run  # type: ignore[method-assign]
    channel.get_file = fake_get_file  # type: ignore[method-assign]

    destination = channel._get_directory_legacy("/tmp/source", tmp_path / "download")

    assert downloads == ["/tmp/smolvm-dir.DEF456.tar"]
    assert (destination / "note.txt").read_text() == "hello"
    assert commands[0].startswith("mktemp /tmp/smolvm-dir.")
    assert commands[1] == "set -e; tar -cf /tmp/smolvm-dir.DEF456.tar -C /tmp/source ."
    assert commands[-1] == "rm -f -- /tmp/smolvm-dir.DEF456.tar"


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

    channel = FakeRustChannel([_set, _get, _get, _delete])
    assert channel.set_env_vars({"FOO": "bar"}) == ["FOO"]
    assert channel.list_env_vars() == {"FOO": "bar"}
    assert channel.unset_env_vars(["FOO"]) == {"FOO": "bar"}


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
