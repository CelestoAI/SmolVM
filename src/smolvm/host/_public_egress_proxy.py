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

"""Host-owned proxy implementation for the private public-egress layer."""

from __future__ import annotations

import re
import select
import socket
import socketserver
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from typing import TypeAlias
from urllib.parse import SplitResult, urlsplit

from smolvm.host._public_egress import (
    AddressResolver,
    EgressBlockReason,
    EgressDecision,
    EgressRequest,
    EgressRequestKind,
    EgressTransport,
    decide_public_egress,
)

_MAX_HEADER_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_DEFAULT_CONNECT_TIMEOUT = 10.0
_DEFAULT_IDLE_TIMEOUT = 30.0
_HEADER_NAME = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_HTTPS_CONNECT_PORTS = frozenset({443})

Connector: TypeAlias = Callable[[str, int, float], socket.socket]
DecisionMaker: TypeAlias = Callable[[EgressRequest], EgressDecision]


@dataclass(frozen=True)
class PublicProxyEndpoint:
    """Host listener owned by one proxy instance."""

    host: str
    port: int


@dataclass(frozen=True)
class _ParsedRequest:
    method: str
    target: str
    version: str
    header_lines: tuple[bytes, ...]
    body_prefix: bytes

    def header(self, name: bytes) -> bytes | None:
        values = self.headers(name)
        return values[0] if values else None

    def headers(self, name: bytes) -> tuple[bytes, ...]:
        prefix = name.lower() + b":"
        values: list[bytes] = []
        for line in self.header_lines:
            if line.lower().startswith(prefix):
                values.append(line.split(b":", 1)[1].strip())
        return tuple(values)


class _ThreadingProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        owner = self.server.owner  # type: ignore[attr-defined]
        owner._handle_client(self.request)  # noqa: SLF001


class PublicEgressProxy:
    """A fail-closed HTTP/CONNECT proxy for one sandbox session.

    The proxy resolves every new request through the public-egress contract and
    opens the upstream socket using only the returned pinned address.  It does
    not expose authentication or a public network mode; guest reachability is
    enforced separately by the backend adapter.
    """

    def __init__(
        self,
        session_id: str,
        *,
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
        resolver: AddressResolver | None = None,
        connector: Connector | None = None,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        if not session_id:
            raise ValueError("session_id cannot be empty")
        if not 0 <= listen_port <= 65535:
            raise ValueError("listen_port must be between 0 and 65535")
        self.session_id = session_id
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._resolver = resolver
        self._connector = connector or _connect_pinned
        self._connect_timeout = connect_timeout
        self._idle_timeout = idle_timeout
        self._server: _ThreadingProxyServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def endpoint(self) -> PublicProxyEndpoint:
        """Return the bound listener after :meth:`start`."""
        if self._server is None:
            raise RuntimeError("public-egress proxy is not running")
        host, port = self._server.server_address[:2]
        return PublicProxyEndpoint(str(host), int(port))

    @property
    def running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> PublicProxyEndpoint:
        """Bind and start the proxy, failing before a guest can use it."""
        if self._server is not None:
            return self.endpoint
        server = _ThreadingProxyServer((self._listen_host, self._listen_port), _ProxyHandler)
        server.owner = self  # type: ignore[attr-defined]
        thread = threading.Thread(
            target=server.serve_forever,
            name=f"smolvm-egress-{self.session_id}",
            daemon=True,
        )
        try:
            thread.start()
            if not thread.is_alive():
                raise RuntimeError("public-egress proxy did not start")
        except BaseException:
            server.server_close()
            raise
        self._server = server
        self._thread = thread
        return self.endpoint

    def stop(self) -> None:
        """Stop the listener and release its port. Safe to call repeatedly."""
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def __enter__(self) -> PublicEgressProxy:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()

    def _decide(self, request: EgressRequest) -> EgressDecision:
        return decide_public_egress(request, resolver=self._resolver)

    def _handle_client(self, client: socket.socket) -> None:
        client.settimeout(self._idle_timeout)
        try:
            request = _read_request(client)
            if request.method == "CONNECT":
                self._handle_connect(client, request)
            else:
                self._handle_http(client, request)
        except _ProxyRequestError as exc:
            _send_error(client, exc.status, exc.reason)
        except (OSError, TimeoutError):
            _send_error(client, HTTPStatus.BAD_GATEWAY, "Upstream connection failed.")

    def _handle_connect(self, client: socket.socket, request: _ParsedRequest) -> None:
        hostname, port = _parse_authority(request.target)
        if port not in _HTTPS_CONNECT_PORTS:
            raise _ProxyRequestError(
                HTTPStatus.FORBIDDEN,
                f"Connection blocked: {EgressBlockReason.UNSUPPORTED_TRANSPORT.value}.",
            )
        decision = self._decide(
            EgressRequest(
                hostname=hostname,
                port=port,
                transport=EgressTransport.HTTPS,
                kind=EgressRequestKind.SUBRESOURCE,
            )
        )
        upstream = self._open_upstream(decision)
        with upstream:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if request.body_prefix:
                upstream.sendall(request.body_prefix)
            _relay_bidirectional(client, upstream, self._idle_timeout)

    def _handle_http(self, client: socket.socket, request: _ParsedRequest) -> None:
        parsed = _parse_absolute_http_target(request.target)
        remaining_body = _request_body_remaining(request)
        assert parsed.hostname is not None
        transport = (
            EgressTransport.WEBSOCKET
            if (request.header(b"upgrade") or b"").lower() == b"websocket"
            else EgressTransport.HTTP
        )
        decision = self._decide(
            EgressRequest(
                hostname=parsed.hostname,
                port=parsed.port or 80,
                transport=transport,
                kind=(
                    EgressRequestKind.WEBSOCKET
                    if transport is EgressTransport.WEBSOCKET
                    else EgressRequestKind.SUBRESOURCE
                ),
            )
        )
        upstream = self._open_upstream(decision)
        with upstream:
            upstream.settimeout(self._idle_timeout)
            upstream.sendall(_origin_form_request(request, parsed))
            _forward_request_body(client, upstream, remaining_body)
            if transport is EgressTransport.WEBSOCKET:
                _relay_bidirectional(client, upstream, self._idle_timeout)
            else:
                _relay_response(upstream, client)

    def _open_upstream(self, decision: EgressDecision) -> socket.socket:
        if not decision.allowed or decision.endpoint is None:
            reason = decision.block_reason or EgressBlockReason.NON_GLOBAL
            raise _ProxyRequestError(
                HTTPStatus.FORBIDDEN,
                f"Connection blocked: {reason.value}.",
            )
        endpoint = decision.endpoint
        try:
            return self._connector(
                str(endpoint.pinned_address),
                endpoint.port,
                self._connect_timeout,
            )
        except OSError as exc:
            raise _ProxyRequestError(
                HTTPStatus.BAD_GATEWAY,
                "Upstream connection failed.",
            ) from exc


class _ProxyRequestError(Exception):
    def __init__(self, status: HTTPStatus, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _connect_pinned(address: str, port: int, timeout: float) -> socket.socket:
    """Connect to the already-checked address without another DNS lookup."""
    return socket.create_connection((address, port), timeout=timeout)


def _read_request(client: socket.socket) -> _ParsedRequest:
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        chunk = client.recv(_READ_CHUNK_BYTES)
        if not chunk:
            raise _ProxyRequestError(HTTPStatus.BAD_REQUEST, "Proxy request is incomplete.")
        buffer.extend(chunk)
        if len(buffer) > _MAX_HEADER_BYTES:
            raise _ProxyRequestError(
                HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
                "Headers are too large.",
            )

    raw_headers, body_prefix = bytes(buffer).split(b"\r\n\r\n", 1)
    lines = raw_headers.split(b"\r\n")
    if any(b"\r" in line or b"\n" in line for line in lines):
        raise _ProxyRequestError(HTTPStatus.BAD_REQUEST, "Proxy request is invalid.")
    try:
        method_raw, target_raw, version_raw = lines[0].split(b" ", 2)
        method = method_raw.decode("ascii").upper()
        target = target_raw.decode("ascii")
        version = version_raw.decode("ascii")
    except (UnicodeDecodeError, ValueError) as exc:
        raise _ProxyRequestError(HTTPStatus.BAD_REQUEST, "Proxy request is invalid.") from exc
    if not method or not version.startswith("HTTP/1."):
        raise _ProxyRequestError(HTTPStatus.BAD_REQUEST, "Proxy request is invalid.")
    for line in lines[1:]:
        if line[:1] in {b" ", b"\t"} or b":" not in line:
            raise _ProxyRequestError(HTTPStatus.BAD_REQUEST, "Proxy headers are invalid.")
        name = line.split(b":", 1)[0]
        if _HEADER_NAME.fullmatch(name) is None:
            raise _ProxyRequestError(HTTPStatus.BAD_REQUEST, "Proxy headers are invalid.")
    return _ParsedRequest(method, target, version, tuple(lines[1:]), body_prefix)


def _parse_authority(authority: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(f"//{authority}")
        hostname, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise _ProxyRequestError(HTTPStatus.BAD_REQUEST, "CONNECT target is invalid.") from exc
    if (
        hostname is None
        or port is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise _ProxyRequestError(
            HTTPStatus.BAD_REQUEST,
            "CONNECT target must include host and port.",
        )
    return hostname, port


def _parse_absolute_http_target(target: str) -> SplitResult:
    try:
        parsed = urlsplit(target)
        port = parsed.port
    except ValueError as exc:
        raise _ProxyRequestError(HTTPStatus.BAD_REQUEST, "Proxy target is invalid.") from exc
    if (
        parsed.scheme.lower() != "http"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise _ProxyRequestError(HTTPStatus.BAD_REQUEST, "Proxy target must be an HTTP URL.")
    return parsed


def _origin_form_request(request: _ParsedRequest, target: SplitResult) -> bytes:
    path = target.path or "/"
    if target.query:
        path = f"{path}?{target.query}"
    lines = [f"{request.method} {path} {request.version}".encode("ascii")]
    for line in request.header_lines:
        name = line.split(b":", 1)[0].strip().lower()
        if name in {b"proxy-authorization", b"proxy-connection", b"connection"}:
            continue
        lines.append(line)
    if (request.header(b"upgrade") or b"").lower() != b"websocket":
        lines.append(b"Connection: close")
    else:
        lines.append(b"Connection: Upgrade")
    return b"\r\n".join(lines) + b"\r\n\r\n" + request.body_prefix


def _request_body_remaining(request: _ParsedRequest) -> int:
    transfer_encodings = request.headers(b"transfer-encoding")
    content_lengths = request.headers(b"content-length")
    if transfer_encodings:
        raise _ProxyRequestError(
            HTTPStatus.NOT_IMPLEMENTED,
            "Chunked proxy requests are unsupported.",
        )
    if len(content_lengths) > 1:
        raise _ProxyRequestError(HTTPStatus.BAD_REQUEST, "Content-Length is ambiguous.")
    if not content_lengths:
        return 0
    try:
        expected = int(content_lengths[0])
    except ValueError as exc:
        raise _ProxyRequestError(HTTPStatus.BAD_REQUEST, "Content-Length is invalid.") from exc
    if expected < 0:
        raise _ProxyRequestError(HTTPStatus.BAD_REQUEST, "Content-Length is invalid.")
    remaining = expected - len(request.body_prefix)
    if remaining < 0:
        raise _ProxyRequestError(HTTPStatus.BAD_REQUEST, "Request body is larger than declared.")
    return remaining


def _forward_request_body(client: socket.socket, upstream: socket.socket, remaining: int) -> None:
    while remaining:
        chunk = client.recv(min(_READ_CHUNK_BYTES, remaining))
        if not chunk:
            raise _ProxyRequestError(HTTPStatus.BAD_REQUEST, "Proxy request body is incomplete.")
        upstream.sendall(chunk)
        remaining -= len(chunk)


def _relay_response(upstream: socket.socket, client: socket.socket) -> None:
    while True:
        chunk = upstream.recv(_READ_CHUNK_BYTES)
        if not chunk:
            return
        client.sendall(chunk)


def _relay_bidirectional(left: socket.socket, right: socket.socket, idle_timeout: float) -> None:
    sockets = (left, right)
    while True:
        readable, _, _ = select.select(sockets, (), (), idle_timeout)
        if not readable:
            return
        for source in readable:
            target = right if source is left else left
            data = source.recv(_READ_CHUNK_BYTES)
            if not data:
                return
            target.sendall(data)


def _send_error(client: socket.socket, status: HTTPStatus, reason: str) -> None:
    body = reason.encode("utf-8", errors="replace")
    response = (
        f"HTTP/1.1 {status.value} {status.phrase}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body
    with suppress(OSError):
        client.sendall(response)
