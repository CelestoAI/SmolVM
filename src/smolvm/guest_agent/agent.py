#!/usr/bin/env python3
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

"""SmolVM guest agent — host↔guest control plane over AF_VSOCK.

Runs as a background process inside the guest, started by ``/init`` before
sshd, so the host can run commands, transfer files, and probe readiness
without the guest network or sshd being up.

This file is **standalone and stdlib-only**: it is copied verbatim into the
guest image (where the ``smolvm`` package is not installed), so it must not
import anything from ``smolvm``. It embeds a byte-compatible copy of the
framing in ``smolvm/comm/protocol.py``; ``tests/test_guest_agent.py`` drives
this module with the host-side protocol helpers over a socketpair to prove the
two ends stay in sync.

Security: like SSH-into-your-own-sandbox, anyone who can open the guest's
vsock port gets to run commands as the agent's user. The guest is a disposable
sandbox the host owns, so this matches SmolVM's existing trust model — but the
channel must never be exposed beyond the host.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import selectors
import signal
import socket
import stat
import struct
import sys
import tempfile
import threading
from typing import Any

# ── Wire protocol (kept byte-compatible with smolvm/comm/protocol.py) ──────

PROTOCOL_VERSION = 1
SMOLVM_AGENT_PORT = 1024
CHUNK_SIZE = 64 * 1024
_MAX_FRAME = 16 * 1024 * 1024

FRAME_JSON = 1
FRAME_STDOUT = 2
FRAME_STDERR = 3
FRAME_DATA = 4
FRAME_EOF = 5

_HEADER = struct.Struct(">BI")

AGENT_VERSION = "1.0"

# VMADDR_CID_ANY is 0xFFFFFFFF; expose a fallback for older Pythons that lack
# the named constant even though AF_VSOCK itself is available.
_VMADDR_CID_ANY = getattr(socket, "VMADDR_CID_ANY", 0xFFFFFFFF)


def recv_exact(sock: Any, n: int) -> bytes:
    if n == 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed mid-frame")
        buf.extend(chunk)
    return bytes(buf)


def send_frame(sock: Any, frame_type: int, payload: bytes = b"") -> None:
    if len(payload) > _MAX_FRAME:
        raise ValueError(f"frame payload too large: {len(payload)} bytes")
    sock.sendall(_HEADER.pack(frame_type, len(payload)) + payload)


def recv_frame(sock: Any) -> tuple[int, bytes]:
    frame_type, length = _HEADER.unpack(recv_exact(sock, _HEADER.size))
    if length > _MAX_FRAME:
        raise ValueError(f"frame payload too large: {length} bytes")
    return frame_type, recv_exact(sock, length)


def send_json(sock: Any, obj: dict[str, Any]) -> None:
    send_frame(sock, FRAME_JSON, json.dumps(obj).encode("utf-8"))


def recv_json(sock: Any) -> dict[str, Any]:
    frame_type, payload = recv_frame(sock)
    if frame_type != FRAME_JSON:
        raise ValueError(f"expected JSON control frame, got frame type {frame_type}")
    obj = json.loads(payload.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("control frame is not a JSON object")
    return obj


# ── Request handlers ───────────────────────────────────────────────────────


def _build_argv(command: str, shell: str) -> list[str]:
    """Build the argv for a command, mirroring SSHClient's POSIX wrapping.

    ``login`` runs the command through a login shell (``$SHELL -lc``) so it
    sees ``/etc/profile`` and the injected ``/etc/profile.d/smolvm_env.sh``,
    matching SSH behavior. ``raw`` runs it with no login semantics.
    """
    if shell == "raw":
        return ["/bin/sh", "-c", command]
    shell_bin = os.environ.get("SHELL") or "/bin/sh"
    return [shell_bin, "-lc", command]


def _kill_process_group(proc: Any) -> None:
    """SIGKILL the child's process group and reap it."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)


def handle_ping(conn: Any, req: dict[str, Any]) -> None:
    send_json(
        conn,
        {
            "ok": True,
            "op": "pong",
            "agent_version": AGENT_VERSION,
            "proto": PROTOCOL_VERSION,
        },
    )


def handle_run(conn: Any, req: dict[str, Any]) -> None:
    import subprocess
    import time

    command = req.get("cmd")
    if not command:
        send_json(conn, {"ok": False, "op": "error", "error": "missing 'cmd'"})
        return
    shell = req.get("shell", "login")
    timeout = req.get("timeout")
    env = dict(os.environ)
    for key, value in (req.get("env") or {}).items():
        env[str(key)] = str(value)

    try:
        proc = subprocess.Popen(  # noqa: S603 - intentional command execution in sandbox
            _build_argv(command, shell),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,  # own process group, so timeout can killpg
        )
    except OSError as exc:
        send_json(conn, {"ok": False, "op": "error", "error": f"spawn failed: {exc}"})
        return

    deadline = None if not timeout else time.monotonic() + float(timeout)
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, FRAME_STDOUT)
    sel.register(proc.stderr, selectors.EVENT_READ, FRAME_STDERR)
    open_streams = 2
    timed_out = False
    try:
        while open_streams > 0:
            sel_timeout = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                sel_timeout = remaining
            for key, _mask in sel.select(timeout=sel_timeout):
                chunk = os.read(key.fileobj.fileno(), CHUNK_SIZE)
                if not chunk:
                    sel.unregister(key.fileobj)
                    open_streams -= 1
                    continue
                send_frame(conn, key.data, chunk)
    except Exception:
        # Peer dropped (or a read/send failed) mid-stream: kill and reap the
        # child so it isn't left running and unreaped in the guest, then let
        # handle_connection swallow the (connection) error as usual.
        _kill_process_group(proc)
        raise
    finally:
        sel.close()

    if timed_out:
        _kill_process_group(proc)
        send_json(conn, {"ok": True, "op": "timeout", "timeout": float(timeout)})
        return

    exit_code = proc.wait()
    send_json(conn, {"ok": True, "op": "exit", "exit_code": exit_code})


def handle_put_file(conn: Any, req: dict[str, Any]) -> None:
    path = req.get("path")
    size = int(req.get("size", 0))
    mode = req.get("mode")

    error: str | None = None
    tmp_path: str | None = None
    handle = None
    if not path:
        error = "missing 'path'"
    else:
        target = os.path.abspath(path)
        parent = os.path.dirname(target) or "/"
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".smolvm-put-")
            handle = os.fdopen(tmp_fd, "wb")
        except OSError as exc:
            error = f"cannot create file: {exc}"

    # Always drain exactly `size` payload bytes so the stream stays framed even
    # when the write target is unwritable; discard them if we have no file.
    received = 0
    try:
        while received < size:
            frame_type, payload = recv_frame(conn)
            if frame_type == FRAME_EOF:
                break
            if frame_type != FRAME_DATA:
                error = error or f"unexpected frame type {frame_type} during upload"
                break
            received += len(payload)
            if handle is not None and error is None:
                try:
                    handle.write(payload)
                except OSError as exc:
                    error = str(exc)
    finally:
        if handle is not None:
            handle.close()

    if error is None and tmp_path is not None:
        try:
            if mode is not None:
                os.chmod(tmp_path, int(mode))
            os.replace(tmp_path, os.path.abspath(path))
        except OSError as exc:
            error = str(exc)

    if error is not None and tmp_path is not None:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)

    send_json(conn, {"ok": error is None} if error is None else {"ok": False, "error": error})


def handle_get_file(conn: Any, req: dict[str, Any]) -> None:
    path = req.get("path")
    if not path:
        send_json(conn, {"ok": False, "error": "missing 'path'"})
        return
    try:
        info = os.stat(path)
    except OSError as exc:
        send_json(conn, {"ok": False, "error": str(exc)})
        return
    if not stat.S_ISREG(info.st_mode):
        send_json(conn, {"ok": False, "error": "not a regular file"})
        return
    # Open in its own try so an open error is reported BEFORE the header
    # frame; once the header is sent we're committed to streaming, so a later
    # error can't be reported without desyncing the protocol.
    try:
        handle = open(path, "rb")  # noqa: SIM115 - closed via `with` below
    except OSError as exc:
        send_json(conn, {"ok": False, "error": str(exc)})
        return

    send_json(conn, {"ok": True, "mode": stat.S_IMODE(info.st_mode), "size": info.st_size})
    with handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            send_frame(conn, FRAME_DATA, chunk)
    send_frame(conn, FRAME_EOF)


_HANDLERS = {
    "ping": handle_ping,
    "run": handle_run,
    "put_file": handle_put_file,
    "get_file": handle_get_file,
}


def handle_connection(conn: Any) -> None:
    """Serve a single request on *conn* (one request per connection)."""
    try:
        req = recv_json(conn)
    except (ConnectionError, ValueError, OSError, json.JSONDecodeError):
        return

    version = req.get("v")
    if version not in (None, PROTOCOL_VERSION):
        with contextlib.suppress(OSError):
            send_json(conn, {"ok": False, "error": f"unsupported protocol version {version}"})
        return

    handler = _HANDLERS.get(req.get("op"))
    if handler is None:
        with contextlib.suppress(OSError):
            send_json(conn, {"ok": False, "error": f"unknown op {req.get('op')!r}"})
        return

    try:
        handler(conn, req)
    except (ConnectionError, OSError):
        pass  # peer hung up mid-response; nothing actionable
    except Exception as exc:  # noqa: BLE001 - last-resort error report to the host
        with contextlib.suppress(OSError):
            send_json(conn, {"ok": False, "error": f"agent error: {exc}"})


# ── AF_VSOCK server ──────────────────────────────────────────────────────────


def _serve_one(conn: Any, sem: threading.Semaphore) -> None:
    try:
        handle_connection(conn)
    finally:
        with contextlib.suppress(OSError):
            conn.close()
        sem.release()


def serve(port: int = SMOLVM_AGENT_PORT, max_workers: int = 64) -> None:
    """Listen on ``AF_VSOCK`` and serve requests until killed."""
    if not hasattr(socket, "AF_VSOCK"):
        raise RuntimeError("this platform has no AF_VSOCK support")

    server = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((_VMADDR_CID_ANY, port))
    server.listen(128)
    sys.stderr.write(f"smolvm-guest-agent listening on vsock port {port}\n")
    sys.stderr.flush()

    sem = threading.Semaphore(max_workers)
    while True:
        conn, _peer = server.accept()
        sem.acquire()
        threading.Thread(target=_serve_one, args=(conn, sem), daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SmolVM guest agent (vsock).")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SMOLVM_AGENT_PORT", SMOLVM_AGENT_PORT)),
        help="vsock port to listen on",
    )
    args = parser.parse_args(argv)
    try:
        serve(port=args.port)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
