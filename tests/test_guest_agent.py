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

"""Tests for the guest agent and the vsock wire protocol.

The agent (``smolvm.guest_agent.agent``) ships into the guest as a standalone
file and embeds its own copy of the framing in ``smolvm.comm.protocol``. These
tests drive the agent's per-connection handler over an ``AF_UNIX`` socketpair
using the *host-side* protocol helpers, so any drift between the two framings
is caught immediately — without needing a real VM or AF_VSOCK.
"""

import os
import socket
import threading

import pytest

from smolvm.comm import protocol
from smolvm.guest_agent import agent


def _serve_once(guest_sock: socket.socket) -> threading.Thread:
    """Run the agent's handler for one request on a background thread."""

    def _target() -> None:
        try:
            agent.handle_connection(guest_sock)
        finally:
            guest_sock.close()

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread


def _read_run_response(host_sock: socket.socket) -> tuple[dict, bytes, bytes]:
    """Collect a ``run`` response: stream frames until the terminal JSON."""
    stdout = bytearray()
    stderr = bytearray()
    while True:
        frame_type, payload = protocol.recv_frame(host_sock)
        if frame_type == protocol.FRAME_JSON:
            import json

            return json.loads(payload.decode()), bytes(stdout), bytes(stderr)
        if frame_type == protocol.FRAME_STDOUT:
            stdout.extend(payload)
        elif frame_type == protocol.FRAME_STDERR:
            stderr.extend(payload)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected frame type {frame_type}")


class TestFramingParity:
    """The agent must embed the exact same wire constants as the host."""

    def test_constants_match(self) -> None:
        assert agent.PROTOCOL_VERSION == protocol.PROTOCOL_VERSION
        assert agent.SMOLVM_AGENT_PORT == protocol.SMOLVM_AGENT_PORT
        assert agent.CHUNK_SIZE == protocol.CHUNK_SIZE
        assert agent.FRAME_JSON == protocol.FRAME_JSON
        assert agent.FRAME_STDOUT == protocol.FRAME_STDOUT
        assert agent.FRAME_STDERR == protocol.FRAME_STDERR
        assert agent.FRAME_DATA == protocol.FRAME_DATA
        assert agent.FRAME_EOF == protocol.FRAME_EOF

    def test_frame_roundtrip_over_socketpair(self) -> None:
        a, b = socket.socketpair()
        with a, b:
            protocol.send_frame(a, protocol.FRAME_DATA, b"hello world")
            frame_type, payload = protocol.recv_frame(b)
        assert frame_type == protocol.FRAME_DATA
        assert payload == b"hello world"

    def test_recv_exact_raises_on_short_read(self) -> None:
        a, b = socket.socketpair()
        with b:
            a.sendall(b"abc")
            a.close()
            with pytest.raises(ConnectionError):
                protocol.recv_exact(b, 8)


class TestPing:
    def test_ping_returns_pong(self) -> None:
        host, guest = socket.socketpair()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(host, {"v": protocol.PROTOCOL_VERSION, "op": "ping"})
            resp = protocol.recv_json(host)
            thread.join(timeout=5)
        assert resp["ok"] is True
        assert resp["op"] == "pong"
        assert resp["proto"] == protocol.PROTOCOL_VERSION
        assert "agent_version" in resp


class TestRun:
    def test_run_raw_captures_stdout_and_exit(self) -> None:
        host, guest = socket.socketpair()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(
                host,
                {"op": "run", "cmd": "printf 'hi there'", "shell": "raw", "timeout": 10},
            )
            terminal, stdout, stderr = _read_run_response(host)
            thread.join(timeout=5)
        assert terminal["op"] == "exit"
        assert terminal["exit_code"] == 0
        assert stdout == b"hi there"
        assert stderr == b""

    def test_run_reports_stderr_and_nonzero_exit(self) -> None:
        host, guest = socket.socketpair()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(
                host,
                {"op": "run", "cmd": "echo oops 1>&2; exit 3", "shell": "raw", "timeout": 10},
            )
            terminal, stdout, stderr = _read_run_response(host)
            thread.join(timeout=5)
        assert terminal["exit_code"] == 3
        assert stderr.strip() == b"oops"

    def test_run_login_shell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force a quiet POSIX shell so login-shell profile noise doesn't
        # pollute stdout on dev machines whose $SHELL is zsh/bash.
        monkeypatch.setenv("SHELL", "/bin/sh")
        host, guest = socket.socketpair()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(
                host,
                {"op": "run", "cmd": "printf done", "shell": "login", "timeout": 10},
            )
            terminal, stdout, _ = _read_run_response(host)
            thread.join(timeout=5)
        assert terminal["exit_code"] == 0
        assert b"done" in stdout

    def test_run_timeout_kills_and_reports(self) -> None:
        host, guest = socket.socketpair()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(
                host,
                {"op": "run", "cmd": "sleep 10", "shell": "raw", "timeout": 1},
            )
            terminal, _, _ = _read_run_response(host)
            thread.join(timeout=10)
        assert terminal["op"] == "timeout"


class TestFileTransfer:
    def test_put_file_preserves_mode(self, tmp_path) -> None:
        target = tmp_path / "id_ed25519"
        payload = b"-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n"
        host, guest = socket.socketpair()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(
                host,
                {"op": "put_file", "path": str(target), "mode": 0o600, "size": len(payload)},
            )
            protocol.send_frame(host, protocol.FRAME_DATA, payload)
            resp = protocol.recv_json(host)
            thread.join(timeout=5)
        assert resp["ok"] is True
        assert target.read_bytes() == payload
        assert (target.stat().st_mode & 0o777) == 0o600

    def test_put_file_into_missing_dir_fails_cleanly(self, tmp_path) -> None:
        target = tmp_path / "nope" / "file.txt"
        payload = b"data"
        host, guest = socket.socketpair()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(
                host,
                {"op": "put_file", "path": str(target), "mode": None, "size": len(payload)},
            )
            # Bytes must still be drained even though the write target is bad.
            protocol.send_frame(host, protocol.FRAME_DATA, payload)
            resp = protocol.recv_json(host)
            thread.join(timeout=5)
        assert resp["ok"] is False
        assert "error" in resp

    def test_put_file_lands_inside_directory(self, tmp_path) -> None:
        # Destination is an existing directory: the file should land inside it
        # under the source basename (`name`), matching `cp file dir/`.
        payload = b"inside-the-dir"
        host, guest = socket.socketpair()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(
                host,
                {
                    "op": "put_file",
                    "path": str(tmp_path),
                    "name": "report.md",
                    "mode": None,
                    "size": len(payload),
                },
            )
            protocol.send_frame(host, protocol.FRAME_DATA, payload)
            resp = protocol.recv_json(host)
            thread.join(timeout=5)
        assert resp["ok"] is True
        landed = tmp_path / "report.md"
        assert landed.read_bytes() == payload

    def test_put_file_into_directory_strips_traversal(self, tmp_path) -> None:
        # A malicious/buggy `name` must not escape the destination directory:
        # basename() collapses "../escape" to "escape".
        dest = tmp_path / "dest"
        dest.mkdir()
        payload = b"no-escape"
        host, guest = socket.socketpair()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(
                host,
                {
                    "op": "put_file",
                    "path": str(dest),
                    "name": "../escape",
                    "mode": None,
                    "size": len(payload),
                },
            )
            protocol.send_frame(host, protocol.FRAME_DATA, payload)
            resp = protocol.recv_json(host)
            thread.join(timeout=5)
        assert resp["ok"] is True
        assert (dest / "escape").read_bytes() == payload
        assert not (tmp_path / "escape").exists()

    @pytest.mark.parametrize("name", [".", ".."])
    def test_put_file_into_directory_rejects_dot_names(self, tmp_path, name) -> None:
        # "." and ".." survive basename() and would join back to a directory,
        # failing later with a cryptic Errno 21. They're rejected up front with
        # the same clear message as a missing name.
        dest = tmp_path / "dest"
        dest.mkdir()
        payload = b"data"
        host, guest = socket.socketpair()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(
                host,
                {
                    "op": "put_file",
                    "path": str(dest),
                    "name": name,
                    "mode": None,
                    "size": len(payload),
                },
            )
            # Bytes must still be drained so the stream stays framed.
            protocol.send_frame(host, protocol.FRAME_DATA, payload)
            resp = protocol.recv_json(host)
            thread.join(timeout=5)
        assert resp["ok"] is False
        assert "invalid" in resp["error"]
        assert name in resp["error"]

    def test_put_file_into_directory_without_name_errors(self, tmp_path) -> None:
        # A directory destination with no usable filename fails with a clear
        # message instead of a cryptic "Is a directory" errno later.
        payload = b"data"
        host, guest = socket.socketpair()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(
                host,
                {"op": "put_file", "path": str(tmp_path), "mode": None, "size": len(payload)},
            )
            # Bytes must still be drained so the stream stays framed.
            protocol.send_frame(host, protocol.FRAME_DATA, payload)
            resp = protocol.recv_json(host)
            thread.join(timeout=5)
        assert resp["ok"] is False
        assert "directory" in resp["error"]

    def test_get_file_streams_bytes_and_mode(self, tmp_path) -> None:
        source = tmp_path / "blob.bin"
        payload = os.urandom(200_000)  # spans multiple CHUNK_SIZE frames
        source.write_bytes(payload)
        os.chmod(source, 0o640)

        host, guest = socket.socketpair()
        received = bytearray()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(host, {"op": "get_file", "path": str(source)})
            header = protocol.recv_json(host)
            while True:
                frame_type, chunk = protocol.recv_frame(host)
                if frame_type == protocol.FRAME_EOF:
                    break
                assert frame_type == protocol.FRAME_DATA
                received.extend(chunk)
            thread.join(timeout=5)
        assert header["ok"] is True
        assert header["size"] == len(payload)
        assert header["mode"] == 0o640
        assert bytes(received) == payload

    def test_get_missing_file_returns_error(self, tmp_path) -> None:
        host, guest = socket.socketpair()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(host, {"op": "get_file", "path": str(tmp_path / "absent")})
            resp = protocol.recv_json(host)
            thread.join(timeout=5)
        assert resp["ok"] is False


class TestProtocolErrors:
    def test_unknown_op(self) -> None:
        host, guest = socket.socketpair()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(host, {"op": "frobnicate"})
            resp = protocol.recv_json(host)
            thread.join(timeout=5)
        assert resp["ok"] is False
        assert "unknown op" in resp["error"]

    def test_version_mismatch_rejected(self) -> None:
        host, guest = socket.socketpair()
        with host:
            thread = _serve_once(guest)
            protocol.send_json(host, {"v": 999, "op": "ping"})
            resp = protocol.recv_json(host)
            thread.join(timeout=5)
        assert resp["ok"] is False
        assert "version" in resp["error"]
