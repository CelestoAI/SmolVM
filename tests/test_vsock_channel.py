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

"""Tests for the host-side VsockChannel.

The channel's transport (AF_VSOCK / UDS) is exercised by pointing ``_open`` at
a socketpair whose far end is served by the real guest agent, so these tests
validate the full host↔agent round-trip without a VM.
"""

import queue
import socket
import threading

import pytest

from smolvm.comm import CommChannel
from smolvm.comm.vsock_channel import VsockChannel
from smolvm.exceptions import OperationTimeoutError, SmolVMError
from smolvm.guest_agent import agent


def _agent_backed_open():
    """A ``_open`` replacement: each call is served by one agent handler."""

    def _open() -> socket.socket:
        host, guest = socket.socketpair()

        def _serve() -> None:
            try:
                agent.handle_connection(guest)
            finally:
                guest.close()

        threading.Thread(target=_serve, daemon=True).start()
        return host

    return _open


class TestConstruction:
    def test_satisfies_commchannel(self) -> None:
        assert isinstance(VsockChannel.from_cid(3), CommChannel)
        assert VsockChannel.from_cid(3).kind == "vsock"

    def test_requires_exactly_one_target(self) -> None:
        with pytest.raises(ValueError):
            VsockChannel()
        with pytest.raises(ValueError):
            VsockChannel(guest_cid=3, uds_path="/tmp/x.sock")


class TestRun:
    def test_run_raw_captures_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ch = VsockChannel.from_cid(3)
        monkeypatch.setattr(ch, "_open", _agent_backed_open())
        result = ch.run("printf 'hi there'", shell="raw")
        assert result.exit_code == 0
        assert result.stdout == "hi there"
        assert result.ok

    def test_run_nonzero_exit_and_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ch = VsockChannel.from_cid(3)
        monkeypatch.setattr(ch, "_open", _agent_backed_open())
        result = ch.run("echo bad 1>&2; exit 7", shell="raw")
        assert result.exit_code == 7
        assert result.stderr.strip() == "bad"

    def test_run_timeout_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ch = VsockChannel.from_cid(3)
        monkeypatch.setattr(ch, "_open", _agent_backed_open())
        with pytest.raises(OperationTimeoutError):
            ch.run("sleep 10", shell="raw", timeout=1)

    def test_run_rejects_empty(self) -> None:
        ch = VsockChannel.from_cid(3)
        with pytest.raises(ValueError):
            ch.run("   ")


class TestFileTransfer:
    def test_put_then_get_roundtrip_preserves_mode(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        local = tmp_path / "key"
        local.write_bytes(b"secret-key-bytes")
        local.chmod(0o600)
        remote = tmp_path / "guest_copy"

        ch = VsockChannel.from_cid(3)
        monkeypatch.setattr(ch, "_open", _agent_backed_open())
        ch.put_file(local, str(remote))
        assert remote.read_bytes() == b"secret-key-bytes"
        assert (remote.stat().st_mode & 0o777) == 0o600

        back = tmp_path / "downloaded"
        ch.get_file(str(remote), back)
        assert back.read_bytes() == b"secret-key-bytes"
        assert (back.stat().st_mode & 0o777) == 0o600

    def test_get_missing_file_raises(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        ch = VsockChannel.from_cid(3)
        monkeypatch.setattr(ch, "_open", _agent_backed_open())
        with pytest.raises(SmolVMError):
            ch.get_file(str(tmp_path / "absent"), tmp_path / "out")


class TestReadiness:
    def test_wait_ready_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ch = VsockChannel.from_cid(3)
        monkeypatch.setattr(ch, "_open", _agent_backed_open())
        ch.wait_ready(timeout=5)
        assert ch.connected

    def test_wait_ready_times_out_when_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ch = VsockChannel.from_cid(3)

        def _always_fail() -> socket.socket:
            raise OSError("connection refused")

        monkeypatch.setattr(ch, "_open", _always_fail)
        with pytest.raises(OperationTimeoutError):
            ch.wait_ready(timeout=0.3, interval=0.05)
        assert not ch.connected


class TestUdsTransport:
    """Exercise the real UDS path + Firecracker-style CONNECT handshake."""

    def test_from_uds_handshake_then_ping(self, tmp_path) -> None:
        uds = str(tmp_path / "vsock.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(uds)
        server.listen(1)

        def _serve() -> None:
            conn, _ = server.accept()
            line = b""
            while not line.endswith(b"\n"):
                chunk = conn.recv(1)
                if not chunk:
                    break
                line += chunk
            assert line.startswith(b"CONNECT")
            conn.sendall(b"OK 1234\n")
            agent.handle_connection(conn)
            conn.close()

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        try:
            ch = VsockChannel.from_uds(uds)
            ch.wait_ready(timeout=5)
            assert ch.connected
        finally:
            server.close()
            thread.join(timeout=5)

    def test_from_uds_closes_socket_when_connect_rejected(self, tmp_path) -> None:
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
            ch = VsockChannel.from_uds(uds)
            with pytest.raises(SmolVMError, match="CONNECT handshake failed"):
                ch._open_uds()
            assert closed.get(timeout=2) == b""
        finally:
            server.close()
            thread.join(timeout=5)
