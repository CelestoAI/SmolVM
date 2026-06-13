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

"""Wire protocol for the SmolVM guest-agent vsock control channel.

Every frame is a 1-byte type tag, a ``u32`` big-endian payload length, and
that many payload bytes. Control frames carry UTF-8 JSON; stream and file
frames carry raw bytes, so command output and large files never pay base64
overhead.

This module is the host-side authority for the legacy framed Python agent
format. Current published images use the Rust HTTP/vsock agent; this framed
protocol remains only as a migration fallback for older images. The Python
agent (``smolvm/guest_agent/agent.py``) embeds a byte-compatible copy of the
framing below. ``tests/test_guest_agent.py`` drives the agent with these host
functions over a socketpair to prove the two ends interoperate while the
fallback remains supported.

Each request occupies its own connection: the host opens a connection, sends
one request frame, consumes the response (which may stream many frames), and
closes. vsock connect is cheap and the agent is unauthenticated, so there is
no handshake to amortize — one request per connection keeps the stream
impossible to desync.

Request/response shapes (``op`` selects the operation, ``v`` the protocol
version):

- ``ping``  → ``{"ok": true, "op": "pong", "agent_version": str, "proto": int}``
- ``run``   ``{"cmd": str, "shell": "login"|"raw", "timeout": int|null, "env": {}}``
  → interleaved ``STDOUT``/``STDERR`` frames, terminated by a control frame
  ``{"op": "exit", "exit_code": int}`` or ``{"op": "timeout", "timeout": float}``.
- ``put_file`` ``{"path": str, "name"?: str, "mode": int|null, "size": int}``
  followed by exactly ``size`` bytes in ``DATA`` frames →
  ``{"ok": bool, "error"?: str}``. When ``path`` is an existing directory the
  guest lands the file inside it using ``name`` (the source basename); only
  the guest can see its own filesystem, so the directory check happens there.
- ``get_file`` ``{"path": str}`` → ``{"ok": true, "mode": int, "size": int}``
  then ``DATA`` frames terminated by ``EOF`` (or ``{"ok": false, "error": str}``).
"""

from __future__ import annotations

import json
import struct
from typing import Any

PROTOCOL_VERSION = 1
"""Bumped on incompatible wire changes; carried as ``v`` on every request."""

SMOLVM_AGENT_PORT = 1024
"""Guest vsock port the agent listens on (host dials the same fixed port)."""

CHUNK_SIZE = 64 * 1024
"""Bytes per streamed output/file frame."""

_MAX_FRAME = 16 * 1024 * 1024
"""Hard cap on a single frame payload, to bound memory on a hostile peer."""

# Frame type tags.
FRAME_JSON = 1
"""A UTF-8 JSON control object."""
FRAME_STDOUT = 2
"""A raw chunk of a command's standard output."""
FRAME_STDERR = 3
"""A raw chunk of a command's standard error."""
FRAME_DATA = 4
"""A raw chunk of file content."""
FRAME_EOF = 5
"""End-of-stream marker (empty payload)."""

_HEADER = struct.Struct(">BI")  # 1-byte type tag + u32 big-endian length


def recv_exact(sock: Any, n: int) -> bytes:
    """Read exactly *n* bytes from *sock*, raising if the peer closes early."""
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
    """Send a single framed message."""
    if len(payload) > _MAX_FRAME:
        raise ValueError(f"frame payload too large: {len(payload)} bytes")
    sock.sendall(_HEADER.pack(frame_type, len(payload)) + payload)


def recv_frame(sock: Any) -> tuple[int, bytes]:
    """Receive a single framed message as ``(frame_type, payload)``."""
    frame_type, length = _HEADER.unpack(recv_exact(sock, _HEADER.size))
    if length > _MAX_FRAME:
        raise ValueError(f"frame payload too large: {length} bytes")
    return frame_type, recv_exact(sock, length)


def send_json(sock: Any, obj: dict[str, Any]) -> None:
    """Send a JSON control frame."""
    send_frame(sock, FRAME_JSON, json.dumps(obj).encode("utf-8"))


def recv_json(sock: Any) -> dict[str, Any]:
    """Receive a JSON control frame, rejecting any other frame type."""
    frame_type, payload = recv_frame(sock)
    if frame_type != FRAME_JSON:
        raise ValueError(f"expected JSON control frame, got frame type {frame_type}")
    obj = json.loads(payload.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("control frame is not a JSON object")
    return obj
