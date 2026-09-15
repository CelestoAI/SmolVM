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

"""Deterministic tests for the host-owned public-egress proxy."""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Sequence

import pytest

from smolvm.host._public_egress_proxy import PublicEgressProxy


class StaticResolver:
    def __init__(self, answers: Sequence[str]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, int]] = []

    def resolve(self, hostname: str, port: int) -> Sequence[str]:
        self.calls.append((hostname, port))
        return self.answers


class SocketPairConnector:
    def __init__(self, handler: Callable[[socket.socket], None]) -> None:
        self.handler = handler
        self.calls: list[tuple[str, int, float]] = []
        self.threads: list[threading.Thread] = []

    def __call__(self, address: str, port: int, timeout: float) -> socket.socket:
        self.calls.append((address, port, timeout))
        proxy_side, upstream_side = socket.socketpair()
        thread = threading.Thread(target=self._run, args=(upstream_side,), daemon=True)
        thread.start()
        self.threads.append(thread)
        return proxy_side

    def _run(self, sock: socket.socket) -> None:
        with sock:
            self.handler(sock)

    def join(self) -> None:
        for thread in self.threads:
            thread.join(timeout=2)


def _recv_until(sock: socket.socket, marker: bytes) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def test_connect_tunnels_to_the_pinned_address() -> None:
    received = bytearray()

    def upstream(sock: socket.socket) -> None:
        received.extend(sock.recv(5))
        sock.sendall(b"world")

    resolver = StaticResolver(["1.1.1.1", "8.8.8.8"])
    connector = SocketPairConnector(upstream)
    with (
        PublicEgressProxy(
            "connect-test",
            resolver=resolver,
            connector=connector,
            idle_timeout=2,
        ) as proxy,
        socket.create_connection((proxy.endpoint.host, proxy.endpoint.port)) as client,
    ):
        client.sendall(b"CONNECT public.test:443 HTTP/1.1\r\nHost: public.test:443\r\n\r\nhello")
        response = _recv_until(client, b"\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 200")
        assert client.recv(5) == b"world"

    connector.join()
    assert received == b"hello"
    assert resolver.calls == [("public.test", 443)]
    assert connector.calls == [("1.1.1.1", 443, 10.0)]


@pytest.mark.parametrize("port", [25, 53, 80, 8443])
def test_connect_rejects_non_https_ports_before_dns_or_connect(port: int) -> None:
    resolver = StaticResolver(["1.1.1.1"])

    def forbidden_connector(_address: str, _port: int, _timeout: float) -> socket.socket:
        raise AssertionError("non-HTTPS CONNECT reached connector")

    with (
        PublicEgressProxy(
            "connect-port-test",
            resolver=resolver,
            connector=forbidden_connector,
        ) as proxy,
        socket.create_connection((proxy.endpoint.host, proxy.endpoint.port)) as client,
    ):
        client.sendall(
            f"CONNECT public.test:{port} HTTP/1.1\r\nHost: public.test:{port}\r\n\r\n".encode()
        )
        response = _recv_until(client, b"unsupported_transport")
        assert response.startswith(b"HTTP/1.1 403")

    assert resolver.calls == []


def test_http_request_is_rewritten_and_proxy_credentials_are_removed() -> None:
    received = bytearray()

    def upstream(sock: socket.socket) -> None:
        received.extend(_recv_until(sock, b"\r\n\r\n"))
        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")

    connector = SocketPairConnector(upstream)
    with (
        PublicEgressProxy(
            "http-test",
            resolver=StaticResolver(["8.8.8.8"]),
            connector=connector,
        ) as proxy,
        socket.create_connection((proxy.endpoint.host, proxy.endpoint.port)) as client,
    ):
        client.sendall(
            b"GET http://public.test/path?q=1 HTTP/1.1\r\n"
            b"Host: public.test\r\n"
            b"Proxy-Authorization: secret\r\n"
            b"Proxy-Connection: keep-alive\r\n\r\n"
        )
        response = _recv_until(client, b"ok")

    connector.join()
    assert response.endswith(b"ok")
    assert received.startswith(b"GET /path?q=1 HTTP/1.1\r\n")
    assert b"Proxy-Authorization" not in received
    assert b"Proxy-Connection" not in received
    assert b"Connection: close\r\n" in received
    assert connector.calls == [("8.8.8.8", 80, 10.0)]


def test_private_and_mixed_answers_fail_before_connecting() -> None:
    def forbidden_connector(_address: str, _port: int, _timeout: float) -> socket.socket:
        raise AssertionError("blocked target reached connector")

    for answers, reason in [
        (["127.0.0.1"], b"loopback"),
        (["1.1.1.1", "10.0.0.1"], b"mixed_address_scope"),
    ]:
        with (
            PublicEgressProxy(
                "blocked-test",
                resolver=StaticResolver(answers),
                connector=forbidden_connector,
            ) as proxy,
            socket.create_connection((proxy.endpoint.host, proxy.endpoint.port)) as client,
        ):
            client.sendall(b"CONNECT blocked.test:443 HTTP/1.1\r\nHost: blocked.test:443\r\n\r\n")
            response = _recv_until(client, b"Connection blocked")
            assert response.startswith(b"HTTP/1.1 403")
            assert reason in response


def test_chunked_request_is_rejected_before_dns_or_connect() -> None:
    resolver = StaticResolver(["1.1.1.1"])

    def forbidden_connector(_address: str, _port: int, _timeout: float) -> socket.socket:
        raise AssertionError("invalid request reached connector")

    with (
        PublicEgressProxy(
            "chunked-test",
            resolver=resolver,
            connector=forbidden_connector,
        ) as proxy,
        socket.create_connection((proxy.endpoint.host, proxy.endpoint.port)) as client,
    ):
        client.sendall(
            b"POST http://public.test/upload HTTP/1.1\r\n"
            b"Host: public.test\r\nTransfer-Encoding: chunked\r\n\r\n"
        )
        response = _recv_until(client, b"unsupported")
        assert response.startswith(b"HTTP/1.1 501")
    assert resolver.calls == []


@pytest.mark.parametrize(
    "framing_header",
    [b"Content-Length : 4", b"Transfer-Encoding : chunked", b"Content\t-Length: 4"],
)
def test_invalid_framing_header_name_is_rejected_before_forwarding(
    framing_header: bytes,
) -> None:
    resolver = StaticResolver(["1.1.1.1"])

    def forbidden_connector(_address: str, _port: int, _timeout: float) -> socket.socket:
        raise AssertionError("ambiguous framing reached connector")

    with (
        PublicEgressProxy(
            "framing-test",
            resolver=resolver,
            connector=forbidden_connector,
        ) as proxy,
        socket.create_connection((proxy.endpoint.host, proxy.endpoint.port)) as client,
    ):
        client.sendall(
            b"POST http://public.test/upload HTTP/1.1\r\n"
            b"Host: public.test\r\n" + framing_header + b"\r\n\r\nPING"
        )
        response = _recv_until(client, b"invalid")
        assert response.startswith(b"HTTP/1.1 400")

    assert resolver.calls == []


@pytest.mark.parametrize(
    "raw_request",
    [
        b"GET http://public.test/ HTTP/1.1\nX-Smuggled: true\r\nHost: public.test\r\n\r\n",
        b"GET http://public.test/ HTTP/1.1\r\nHost: public.test\nX-Smuggled: true\r\n\r\n",
        b"GET http://public.test/ HTTP/1.1\r\nHost: public.test\rX-Smuggled: true\r\n\r\n",
    ],
)
def test_non_crlf_line_endings_are_rejected_before_forwarding(raw_request: bytes) -> None:
    resolver = StaticResolver(["1.1.1.1"])

    def forbidden_connector(_address: str, _port: int, _timeout: float) -> socket.socket:
        raise AssertionError("ambiguous line framing reached connector")

    with (
        PublicEgressProxy(
            "line-framing-test",
            resolver=resolver,
            connector=forbidden_connector,
        ) as proxy,
        socket.create_connection((proxy.endpoint.host, proxy.endpoint.port)) as client,
    ):
        client.sendall(raw_request)
        response = _recv_until(client, b"\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 400")

    assert resolver.calls == []


def test_proxy_lifecycle_is_idempotent_and_releases_listener() -> None:
    proxy = PublicEgressProxy("lifecycle-test")
    endpoint = proxy.start()
    assert proxy.running is True
    assert proxy.start() == endpoint
    proxy.stop()
    proxy.stop()
    assert proxy.running is False
    with socket.socket() as replacement:
        replacement.bind((endpoint.host, endpoint.port))
