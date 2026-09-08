"""Exercise the candidate through TCP, not fabricated HTTPFlow objects.

The loopback dial adapter is test-only: these are proxy-engine tests, not proof
of the Linux firewall, destination-IP filtering, or production readiness.
"""

import asyncio
from contextlib import asynccontextmanager, suppress

import pytest
from candidate import make_proxy


@asynccontextmanager
async def running_proxy(tmp_path, upstream_ca=None):
    master, policy = make_proxy(
        frozenset({"allowed.example"}),
        tmp_path,
        # Synthetic trusted host inventory; no socket is opened to this address.
        host_addresses=frozenset({"127.0.0.1", "8.8.4.4"}),
        upstream_ca=upstream_ca,
    )
    server = master.addons.get("proxyserver")
    task = asyncio.create_task(master.run())
    try:
        async with asyncio.timeout(5):
            while not list(server.servers) or not list(server.servers)[0].listen_addrs:
                if task.done():
                    await task
                    raise AssertionError("proxy exited before listening")
                await asyncio.sleep(0.01)
        port = list(server.servers)[0].listen_addrs[0][1]
        yield port, policy
    finally:
        master.shutdown()
        await asyncio.wait_for(task, 5)
        # Master.done does not stop listeners for an embedded instance.
        master.options.update(server=False)
        await server.setup_servers()


async def exchange(port, request):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 3)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_disallowed_host_is_denied_before_resolution(tmp_path):
    async with running_proxy(tmp_path) as (port, policy):
        resolved = []

        async def must_not_resolve(host, port):
            resolved.append((host, port))
            raise AssertionError("denied names must not reach DNS")

        policy.dial_address = must_not_resolve
        response = await exchange(
            port,
            b"GET http://denied.example/ HTTP/1.1\r\n"
            b"Host: denied.example\r\nConnection: close\r\n\r\n",
        )
        assert b"200" not in response.split(b"\r\n", 1)[0]
        assert resolved == []


@asynccontextmanager
async def upstream(policy, tls=None, keep_alive=False, raw_response=None):
    """An observable local HTTP server; never contacts an Internet endpoint."""
    requests = []
    connections = []

    async def handle(reader, writer):
        connections.append(True)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                requests.append(head)
                connection = b"keep-alive" if keep_alive else b"close"
                writer.write(
                    raw_response
                    or (
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: "
                        + connection
                        + b"\r\n\r\nok"
                    )
                )
                await writer.drain()
                if not keep_alive:
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=tls)
    address = server.sockets[0].getsockname()

    async def fixture_dial(host, port):
        return address

    policy.dial_address = fixture_dial
    try:
        yield requests, connections
    finally:
        server.close()
        await server.wait_closed()


async def test_allowed_http_reaches_upstream(tmp_path):
    async with running_proxy(tmp_path) as (port, policy), upstream(policy) as (requests, _):
        response = await exchange(
            port,
            b"GET http://allowed.example/hello HTTP/1.1\r\n"
            b"Host: allowed.example\r\nConnection: close\r\n\r\n",
        )
        assert response.endswith(b"ok")
        assert len(requests) == 1
        assert requests[0].startswith(b"GET /hello HTTP/1.1\r\n")


@pytest.mark.parametrize(
    "head",
    [
        b"GET http://allowed.example/ HTTP/1.1\r\nHost: denied.example\r\n",
        b"GET http://denied.example/ HTTP/1.1\r\nHost: allowed.example\r\n",
        b"GET http://allowed.example/ HTTP/1.1\r\n"
        b"Host: allowed.example\r\nHost: allowed.example\r\n",
        b"GET http://allowed.example/ HTTP/1.1\r\n",
        b"GET http://allowed.example/ HTTP/1.1\r\n"
        b"Host: allowed.example\r\nX-Test: a\r\n folded\r\n",
        b"GET http://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\nX-Test: a\x00b\r\n",
        b"GET http://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\nConnection: Host\r\n",
        b"GET http://allowed.example/ HTTP/1.1\r\n"
        b"Host: allowed.example\r\nConnection: Content-Length\r\n",
        b"GET http://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\nUpgrade: websocket\r\n",
        b"POST http://allowed.example/ HTTP/1.1\r\n"
        b"Host: allowed.example\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n",
        b"GET http://allowed.example/ HTTP/1.0\r\nHost: allowed.example\r\n",
    ],
)
async def test_ambiguous_authority_or_headers_never_dial(tmp_path, head):
    async with (
        running_proxy(tmp_path) as (port, policy),
        upstream(policy) as (requests, connections),
    ):
        await exchange(port, head + b"Connection: close\r\n\r\n")
        assert requests == []
        assert connections == []


def upstream_tls_files(tmp_path, hostname="allowed.example"):
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from mitmproxy import certs

    store = certs.CertStore.from_store(tmp_path / "origin", "origin", 2048)
    entry = store.get_cert(hostname, [x509.DNSName(hostname)], None)
    pem = tmp_path / "origin-server.pem"
    pem.write_bytes(
        entry.cert.to_pem()
        + entry.privatekey.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pem)
    return context, tmp_path / "origin" / "origin-ca-cert.pem"


@asynccontextmanager
async def tunnel(port, tmp_path, sni="allowed.example"):
    import ssl

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        assert response.startswith(b"HTTP/1.1 200")
        context = ssl.create_default_context(cafile=tmp_path / "mitmproxy-ca-cert.pem")
        context.set_alpn_protocols(["http/1.1"])
        await writer.start_tls(context, server_hostname=sni, ssl_handshake_timeout=3)
        yield reader, writer
    finally:
        writer.close()
        with suppress(ConnectionError, ssl.SSLError):
            await writer.wait_closed()


async def test_allowed_https_checks_upstream_certificate(tmp_path):
    tls, ca = upstream_tls_files(tmp_path)
    async with (
        running_proxy(tmp_path, ca) as (port, policy),
        upstream(policy, tls) as (requests, _),
    ):
        async with tunnel(port, tmp_path) as (reader, writer):
            writer.write(
                b"GET /hello HTTP/1.1\r\nHost: allowed.example\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 3)
        assert response.endswith(b"ok")
        assert len(requests) == 1


@pytest.mark.parametrize(
    "head",
    [
        b"GET / HTTP/1.1\r\nHost: denied.example\r\n",
        b"GET https://denied.example/ HTTP/1.1\r\nHost: allowed.example\r\n",
        b"GET http://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\n",
        b"GET https://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\n",
        b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\n",
        b"GET / HTTP/1.1\r\nHost: allowed.example\r\nHost: denied.example\r\n",
        b"GET / HTTP/1.1\r\nHost: allowed.example\r\nUpgrade: h2c\r\n",
    ],
)
async def test_inner_https_authority_denied_before_dial(tmp_path, head):
    async with running_proxy(tmp_path) as (port, policy):
        dials = []

        async def forbidden_dial(host, port):
            dials.append((host, port))
            raise AssertionError("unexpected dial")

        policy.dial_address = forbidden_dial
        async with tunnel(port, tmp_path) as (reader, writer):
            writer.write(head + b"Connection: close\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(reader.read(), 3)
        assert dials == []


async def test_sni_mismatch_is_denied_during_tls_before_dial(tmp_path):
    import ssl

    async with running_proxy(tmp_path) as (port, policy):
        dials = []

        async def forbidden_dial(host, port):
            dials.append((host, port))
            raise AssertionError("unexpected dial")

        policy.dial_address = forbidden_dial
        with pytest.raises((ConnectionError, ssl.SSLError)):
            async with tunnel(port, tmp_path, "denied.example"):
                pytest.fail("mismatched SNI completed TLS")
        assert dials == []


@pytest.mark.parametrize("trusted", [True, False])
async def test_wrong_host_or_untrusted_upstream_certificate_fails(tmp_path, trusted):
    tls, ca = upstream_tls_files(tmp_path, "denied.example" if trusted else "allowed.example")
    async with (
        running_proxy(tmp_path, ca if trusted else None) as (port, policy),
        upstream(policy, tls) as (requests, _),
    ):
        async with tunnel(port, tmp_path) as (reader, writer):
            writer.write(b"GET / HTTP/1.1\r\nHost: allowed.example\r\nConnection: close\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 3)
        assert not response.startswith(b"HTTP/1.1 200")
        assert requests == []


@pytest.mark.parametrize("stage", ["http", "connect", "tls", "dns"])
async def test_validation_exception_leaves_denial_in_place(tmp_path, monkeypatch, stage):
    from mitmproxy.addons import tlsconfig

    async with (
        running_proxy(tmp_path) as (port, policy),
        upstream(policy) as (requests, connections),
    ):

        def broken(*args, **kwargs):
            raise RuntimeError("injected policy failure")

        if stage in {"http", "connect"}:
            monkeypatch.setattr(policy, "valid_headers", broken)
        elif stage == "dns":

            async def broken_dns(*args):
                broken()

            monkeypatch.setattr(policy, "dial_address", broken_dns)
        else:
            monkeypatch.setattr(tlsconfig.TlsConfig, "get_cert", broken)

        if stage == "tls":
            import ssl

            with pytest.raises((ConnectionError, ssl.SSLError)):
                async with tunnel(port, tmp_path):
                    pytest.fail("failed TLS setup completed")
        elif stage == "connect":
            await exchange(
                port, b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\n\r\n"
            )
        else:
            await exchange(
                port,
                b"GET http://allowed.example/ HTTP/1.1\r\n"
                b"Host: allowed.example\r\nConnection: close\r\n\r\n",
            )
        assert requests == []
        assert connections == []


async def test_missing_policy_hook_cannot_forward_after_connect_authorization(
    tmp_path, monkeypatch
):
    async with (
        running_proxy(tmp_path) as (port, policy),
        upstream(policy) as (requests, connections),
    ):
        async with tunnel(port, tmp_path) as (reader, writer):
            # CONNECT/TLS already succeeded. No request may inherit permission
            # from it when the per-request policy hook disappears.
            monkeypatch.setattr(policy, "requestheaders", lambda f: None)
            writer.write(b"GET / HTTP/1.1\r\nHost: allowed.example\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(reader.read(), 3)
        assert requests == []
        assert connections == []


async def test_unexpected_101_cannot_open_raw_tunnel(tmp_path):
    response = (
        b"HTTP/1.1 101 Switching Protocols\r\nConnection: upgrade\r\nUpgrade: custom\r\n\r\nsecret"
    )
    async with (
        running_proxy(tmp_path) as (port, policy),
        upstream(policy, raw_response=response),
    ):
        received = await exchange(
            port,
            b"GET http://allowed.example/ HTTP/1.1\r\n"
            b"Host: allowed.example\r\nConnection: close\r\n\r\n",
        )
        assert not received.startswith(b"HTTP/1.1 101")
        assert b"secret" not in received


async def test_sse_arrives_without_waiting_for_upstream_close(tmp_path):
    response = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\ndata: hello\n\n"
    async with (
        running_proxy(tmp_path) as (port, policy),
        upstream(policy, raw_response=response, keep_alive=True),
    ):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"GET http://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\n\r\n")
            await writer.drain()
            received = await asyncio.wait_for(reader.readuntil(b"data: hello\n\n"), 3)
            assert received.startswith(b"HTTP/1.1 200")
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.parametrize(
    "addresses",
    [
        ["224.0.0.1"],
        ["8.8.8.8", "239.255.255.250"],
        ["8.8.4.4"],
        ["8.8.8.8", "8.8.4.4"],
        ["0.0.0.0"],
        ["10.0.0.1"],
        ["172.16.0.1"],
        ["192.168.0.1"],
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["100.64.0.1"],
        ["198.18.0.1"],
        ["192.0.2.1"],
        ["198.51.100.1"],
        ["203.0.113.1"],
        ["240.0.0.1"],
        ["255.255.255.255"],
        ["192.0.0.9"],
        ["192.0.0.10"],
        ["192.31.196.1"],
        ["192.52.193.1"],
        ["192.88.99.1"],
        ["192.175.48.1"],
        ["8.8.8.8", "169.254.169.254"],
        ["169.254.169.254", "8.8.8.8"],
        ["127.1"],
        ["2130706433"],
        ["0x7f000001"],
        ["0177.0.0.1"],
        ["::ffff:127.0.0.1"],
        ["2606:4700:4700::1111"],
        [],
    ],
)
async def test_prohibited_dns_answers_are_rejected_before_socket_creation(
    tmp_path, monkeypatch, addresses
):
    import socket

    async with running_proxy(tmp_path) as (port, _):
        dials = []
        proxy_port = port
        real_open = asyncio.open_connection

        async def resolve(host, port, **kwargs):
            assert host == "allowed.example"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in addresses]

        async def open_connection(host, port, **kwargs):
            if (host, port) == ("127.0.0.1", proxy_port):
                return await real_open(host, port, **kwargs)
            dials.append((host, port))
            raise OSError("test blocks every upstream socket")

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        monkeypatch.setattr(asyncio, "open_connection", open_connection)
        await exchange(
            port,
            b"GET http://allowed.example/ HTTP/1.1\r\n"
            b"Host: allowed.example\r\nConnection: close\r\n\r\n",
        )
        assert dials == []


async def test_numeric_dial_is_pinned_and_new_connections_recheck_dns(tmp_path, monkeypatch):
    """Real HTTP exchange, controlled DNS, and an observed OS socket seam.

    Only the approved public numeric address is translated to the local fixture.
    No public socket can be opened, even if the candidate regresses.
    """
    import socket

    async with running_proxy(tmp_path) as (port, policy):
        production_dial = policy.dial_address
        async with upstream(policy) as (requests, connections):
            origin_address = await policy.dial_address("allowed.example", 80)
            policy.dial_address = production_dial
            answers = ["8.8.8.8"]
            resolutions = []
            dials = []
            proxy_port = port
            real_open = asyncio.open_connection

            async def resolve(host, port, **kwargs):
                resolutions.append((host, port))
                assert host == "allowed.example"  # No second resolution of a hostname.
                assert kwargs == {"family": socket.AF_INET, "type": socket.SOCK_STREAM}
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answers]

            async def open_connection(host, port, **kwargs):
                if (host, port) == ("127.0.0.1", proxy_port):
                    return await real_open(host, port, **kwargs)
                dials.append((host, port))
                if (host, port) != ("8.8.8.8", 80):
                    raise OSError("test forbids this dial")
                return await real_open(*origin_address)

            monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
            monkeypatch.setattr(asyncio, "open_connection", open_connection)
            request = (
                b"GET http://allowed.example/ HTTP/1.1\r\n"
                b"Host: allowed.example\r\nConnection: close\r\n\r\n"
            )
            assert (await exchange(port, request)).endswith(b"ok")
            assert dials == [("8.8.8.8", 80)]
            assert len(requests) == len(connections) == 1

            # Rebinding after a successful connection must not inherit its permission.
            answers[:] = ["169.254.169.254"]
            assert not (await exchange(port, request)).startswith(b"HTTP/1.1 200")
            assert resolutions == [("allowed.example", 80), ("allowed.example", 80)]
            assert dials == [("8.8.8.8", 80)]
            assert len(requests) == len(connections) == 1


@pytest.mark.parametrize("encrypted", [False, True], ids=["http", "https"])
@pytest.mark.parametrize("second_host", ["allowed.example", "denied.example"])
async def test_connection_close_does_not_forward_pipelined_requests(
    tmp_path, encrypted, second_host
):
    tls, ca = upstream_tls_files(tmp_path) if encrypted else (None, None)
    async with (
        running_proxy(tmp_path, ca) as (port, policy),
        upstream(policy, tls, keep_alive=True) as (requests, connections),
    ):

        @asynccontextmanager
        async def client():
            if encrypted:
                async with tunnel(port, tmp_path) as pair:
                    yield pair
            else:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                try:
                    yield reader, writer
                finally:
                    writer.close()
                    await writer.wait_closed()

        target = b"/" if encrypted else b"http://allowed.example/"
        first = b"GET " + target + b"first HTTP/1.1\r\nHost: allowed.example\r\n\r\n"
        second = (
            b"GET " + target + b"second HTTP/1.1\r\nHost: " + second_host.encode() + b"\r\n\r\n"
        )
        async with client() as (reader, writer):
            writer.write(first + second)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 3)
            assert response.endswith(b"ok")
            assert b"connection: close\r\n" in response.lower()
        assert len(connections) == 1
        assert len(requests) == 1
        assert requests[0].startswith(b"GET /first HTTP/1.1\r\n")
        assert b"connection: close\r\n" in requests[0].lower()


@pytest.mark.parametrize("encrypted", [False, True], ids=["http", "https"])
async def test_upload_streams_before_client_finishes_without_changing_body(tmp_path, encrypted):
    import hashlib

    tls, ca = upstream_tls_files(tmp_path) if encrypted else (None, None)
    # Request-looking bytes in a framed body must remain payload, not new requests.
    prefix = b"GET /forbidden HTTP/1.1\r\nHost: denied.example\r\n\r\n" + b"a" * 32768
    remainder = b"b" * (512 * 1024)
    body_size = len(prefix) + len(remainder)
    first_chunk = asyncio.Event()
    finished = asyncio.get_running_loop().create_future()
    handlers = set()

    async def handle(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            digest = hashlib.sha256(await reader.readexactly(len(prefix)))
            first_chunk.set()
            remaining = body_size - len(prefix)
            while remaining:
                chunk = await reader.readexactly(min(8192, remaining))
                digest.update(chunk)
                remaining -= len(chunk)
                await asyncio.sleep(0)  # Give the proxy/client a chance to run.
            finished.set_result((head, digest.hexdigest()))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except Exception as exc:
            if not finished.done():
                finished.set_exception(exc)
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    async with running_proxy(tmp_path, ca) as (port, policy):
        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=tls)

        async def fixture_dial(host, port):
            return server.sockets[0].getsockname()

        policy.dial_address = fixture_dial
        try:

            @asynccontextmanager
            async def client():
                if encrypted:
                    async with tunnel(port, tmp_path) as pair:
                        yield pair
                else:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    try:
                        yield reader, writer
                    finally:
                        writer.close()
                        await writer.wait_closed()

            target = b"/upload" if encrypted else b"http://allowed.example/upload"
            async with client() as (reader, writer):
                writer.write(
                    b"POST " + target + b" HTTP/1.1\r\nHost: allowed.example\r\n"
                    b"Content-Length: " + str(body_size).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + prefix
                )
                await writer.drain()
                # A buffered proxy deadlocks here: the rest is intentionally unsent.
                await asyncio.wait_for(first_chunk.wait(), 3)
                writer.write(remainder)
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), 3)
                assert response.endswith(b"ok")
                head, digest = await asyncio.wait_for(finished, 3)
                assert head.startswith(b"POST /upload HTTP/1.1\r\n")
                assert digest == hashlib.sha256(prefix + remainder).hexdigest()
        finally:
            server.close()
            await server.wait_closed()
            for task in tuple(handlers):
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
            if finished.done() and not finished.cancelled():
                finished.exception()  # Retrieve fixture errors even on client failure.


@pytest.mark.parametrize("encrypted", [False, True], ids=["http", "https"])
async def test_completed_response_releases_upstream_even_if_origin_keeps_alive(tmp_path, encrypted):
    tls, ca = upstream_tls_files(tmp_path) if encrypted else (None, None)
    origin_closed = asyncio.Event()
    async with running_proxy(tmp_path, ca) as (port, policy):

        async def handle(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                # Deliberately ignore Connection: close. The proxy must release
                # its own socket after the framed response without trusting us.
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nok"
                )
                await writer.drain()
                assert await reader.read() == b""
                origin_closed.set()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=tls)

        async def fixture_dial(host, port):
            return server.sockets[0].getsockname()

        policy.dial_address = fixture_dial
        try:

            @asynccontextmanager
            async def client():
                if encrypted:
                    async with tunnel(port, tmp_path) as pair:
                        yield pair
                else:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    try:
                        yield reader, writer
                    finally:
                        writer.close()
                        await writer.wait_closed()

            async with client() as (reader, writer):
                target = b"/" if encrypted else b"http://allowed.example/"
                writer.write(b"GET " + target + b" HTTP/1.1\r\nHost: allowed.example\r\n\r\n")
                await writer.drain()
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
                assert await reader.readexactly(2) == b"ok"
                await asyncio.wait_for(origin_closed.wait(), 1)
                assert b"connection: close\r\n" in head.lower()
                assert await asyncio.wait_for(reader.read(), 1) == b""
        finally:
            server.close()
            await server.wait_closed()


async def test_client_admission_is_bounded_and_disconnect_releases_capacity(tmp_path):
    async with running_proxy(tmp_path) as (port, policy):
        clients = []
        try:
            # CONNECT proves admission without creating any upstream socket.
            for _ in range(32):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                clients.append((reader, writer))
                writer.write(
                    b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\n\r\n"
                )
                await writer.drain()
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
                assert head.startswith(b"HTTP/1.1 200")
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            clients.append((reader, writer))
            # Excess connections close before HTTP or TLS parsing.
            assert await asyncio.wait_for(reader.read(), 1) == b""
            clients[0][1].close()
            await clients[0][1].wait_closed()
            # Observe released admission through a real successful CONNECT.
            async with asyncio.timeout(3):
                while True:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    clients.append((reader, writer))
                    writer.write(
                        b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\n\r\n"
                    )
                    await writer.drain()
                    head = await reader.read(4096)
                    if head.startswith(b"HTTP/1.1 200"):
                        break
                    writer.close()
                    await writer.wait_closed()
                    await asyncio.sleep(0.01)
        finally:
            for _, writer in clients:
                writer.close()
            await asyncio.gather(
                *(writer.wait_closed() for _, writer in clients), return_exceptions=True
            )


@pytest.mark.parametrize(
    "mutation", ["tls_verification", "guard_inventory", "policy", "passthrough", "next_layer"]
)
async def test_runtime_configuration_changes_fail_closed(tmp_path, mutation):
    from mitmproxy import ctx

    tls, ca = upstream_tls_files(
        tmp_path, "denied.example" if mutation == "tls_verification" else "allowed.example"
    )
    async with (
        running_proxy(tmp_path, ca) as (port, policy),
        upstream(policy, tls) as (requests, _),
    ):
        async with tunnel(port, tmp_path) as (reader, writer):
            if mutation == "tls_verification":
                ctx.master.options.update(ssl_insecure=True)
            elif mutation == "guard_inventory":
                ctx.master.addons.remove(ctx.master.addons.get("defaultdeny"))
            elif mutation == "passthrough":
                ctx.master.options.update(ignore_hosts=[".*"])
            elif mutation == "next_layer":
                ctx.master.addons.remove(ctx.master.addons.get("nextlayer"))
            else:
                policy.allowed_domains = frozenset({"allowed.example", "denied.example"})
            writer.write(b"GET / HTTP/1.1\r\nHost: allowed.example\r\nConnection: close\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(reader.read(), 3)
        assert requests == []


async def test_protocol_selection_exception_closes_before_dial(tmp_path, monkeypatch):
    from mitmproxy import ctx

    async with (
        running_proxy(tmp_path) as (port, policy),
        upstream(policy) as (requests, connections),
    ):

        def broken(*args):
            raise RuntimeError("injected layer-selection failure")

        monkeypatch.setattr(ctx.master.addons.get("nextlayer"), "_next_layer", broken)
        response = await exchange(
            port,
            b"GET http://allowed.example/ HTTP/1.1\r\n"
            b"Host: allowed.example\r\nConnection: close\r\n\r\n",
        )
        assert response == b""
        assert requests == connections == []
