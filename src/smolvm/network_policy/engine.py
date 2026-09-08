"""Internal HTTP/TLS policy engine. Import only in the isolated proxy worker.

The host runtime must install the external firewall before exposing its listener.
This module alone does not isolate a sandbox.
"""

import asyncio
import ipaddress
import socket
from pathlib import Path

from mitmproxy import flow, http, options
from mitmproxy.addons import next_layer, proxyserver, tlsconfig
from mitmproxy.master import Master
from mitmproxy.proxy import commands, layer

DENIED = flow.Error.KILLED_MESSAGE

# Globally reachable special-purpose ranges are not ordinary Internet origins.
# `is_global` alone admits these (and multicast). Deny entire containing ranges
# rather than inheriting protocol-specific exceptions from Python's registry.
# https://www.iana.org/assignments/iana-ipv4-special-registry/
SPECIAL_DESTINATIONS = tuple(
    ipaddress.IPv4Network(network)
    for network in (
        "192.0.0.0/24",  # IETF protocol assignments, including PCP/TURN anycast
        "192.31.196.0/24",  # AS112
        "192.52.193.0/24",  # automatic multicast tunneling
        "192.88.99.0/24",  # deprecated 6to4 relay anycast
        "192.175.48.0/24",  # direct-delegation AS112
    )
)


class DeniedLayer(layer.Layer):
    def _handle_event(self, event):
        yield commands.CloseConnection(self.context.client)


class GuardedNextLayer(next_layer.NextLayer):
    name = "nextlayer"

    def __init__(self, policy):
        super().__init__()
        self.policy = policy

    def next_layer(self, data):
        data.layer = DeniedLayer(data.context.fork())
        if not self.policy.configuration_valid():
            return
        try:
            data.layer = None
            super().next_layer(data)
        except BaseException:
            data.layer = DeniedLayer(data.context.fork())
            raise


class DefaultDeny:
    """Independent, unconditional guards run before the fallible policy hooks."""

    def next_layer(self, data):
        data.layer = DeniedLayer(data.context.fork())

    def tls_start_client(self, data):
        data.ssl_conn = None

    def tls_start_server(self, data):
        data.ssl_conn = None

    def client_connected(self, client):
        client.error = DENIED

    def http_connect(self, f):
        f.error = flow.Error(DENIED)

    def requestheaders(self, f):
        f.error = flow.Error(DENIED)

    def server_connect(self, data):
        data.server.error = DENIED

    def responseheaders(self, f):
        f.error = flow.Error(DENIED)


class Policy:
    def __init__(self, allowed_domains: frozenset[str], host_addresses: frozenset[str]):
        self.allowed_domains = allowed_domains
        # Supplied by the trusted launcher, never by guest DNS or HTTP input.
        # Runtime integration must inventory all interfaces and fence on changes.
        if not host_addresses:
            raise ValueError("host address inventory is required")
        self.host_addresses = frozenset(ipaddress.IPv4Address(ip) for ip in host_addresses)
        self.tunnels = {}
        self.approved_targets = {}
        self.clients = set()
        self._master = None

    def seal(self, master):
        if self._master is not None:
            raise RuntimeError("policy configuration is immutable")
        expected = (proxyserver.Proxyserver, DefaultDeny, GuardedNextLayer, GuardedTLS, Policy)
        if tuple(type(addon) for addon in master.addons.chain) != expected:
            raise RuntimeError("required policy inventory is missing")
        # OptManager has keys() but is not an iterable mapping.
        option_names = master.options.keys()
        self._options = {key: getattr(master.options, key) for key in option_names}
        self._inventory = tuple(master.addons.chain)
        hooks = (
            "next_layer",
            "client_connected",
            "client_disconnected",
            "http_connect",
            "requestheaders",
            "responseheaders",
            "server_connect",
            "tls_clienthello",
            "tls_start_client",
            "tls_start_server",
        )
        self._hooks = tuple(
            (addon, name, getattr(addon, name, None)) for addon in self._inventory for name in hooks
        )
        self._intent = (self.allowed_domains, self.host_addresses)
        self._master = master

    def configuration_valid(self):
        if self._master is None:
            return False
        option_names = self._master.options.keys()
        return (
            self._intent == (self.allowed_domains, self.host_addresses)
            and self._inventory == tuple(self._master.addons.chain)
            and all(getattr(addon, name, None) == method for addon, name, method in self._hooks)
            and self._options == {key: getattr(self._master.options, key) for key in option_names}
        )

    def client_connected(self, client):
        client.error = DENIED
        if not self.configuration_valid():
            return
        # Provisional spike budget, not a measured production capacity setting.
        if len(self.clients) >= 32:
            return
        self.clients.add(client.id)
        client.error = None

    @staticmethod
    def authority_matches(value: str, host: str, port: int) -> bool:
        return value.lower() in {host, f"{host}:{port}"}

    def http_connect(self, f: http.HTTPFlow):
        f.error = flow.Error(DENIED)
        if not self.configuration_valid():
            return
        request = f.request
        if f.client_conn.tls or f.client_conn.id in self.tunnels:
            return
        if not self.valid_headers(request):
            return
        if request.host not in self.allowed_domains or request.port != 443:
            return
        if request.authority.lower() != f"{request.host}:443":
            return
        hosts = request.headers.get_all("host")
        if len(hosts) != 1 or not self.authority_matches(hosts[0], request.host, 443):
            return
        self.tunnels[f.client_conn.id] = request.host
        f.error = None

    def client_disconnected(self, client):
        self.clients.discard(client.id)
        self.tunnels.pop(client.id, None)
        self.approved_targets.pop(client.id, None)

    def valid_headers(self, request: http.Request) -> bool:
        if request.http_version != "HTTP/1.1":
            return False
        # The engine preserves obs-fold and arbitrary value bytes on output.
        # Reject them instead of asking the origin to interpret them the same way.
        if any(byte < 32 or byte == 127 for _, value in request.headers.fields for byte in value):
            return False
        if "upgrade" in request.headers or "trailer" in request.headers:
            return False
        return all(
            token.strip().lower() in {"close", "keep-alive"}
            for value in request.headers.get_all("connection")
            for token in value.split(",")
        )

    def requestheaders(self, f: http.HTTPFlow):
        # Set denial before fallible validation. Mitmproxy suppresses addon
        # exceptions; they must not turn an incomplete check into permission.
        f.error = flow.Error(DENIED)
        if not self.configuration_valid():
            return
        request = f.request
        if not self.valid_headers(request):
            return
        if request.host not in self.allowed_domains:
            return
        tunnel_host = self.tunnels.get(f.client_conn.id)
        if tunnel_host is not None:
            if not f.client_conn.tls_established or f.client_conn.sni != tunnel_host:
                return
            if (request.host, request.port, request.scheme) != (tunnel_host, 443, "https"):
                return
            # The engine rewrites the inner scheme/host before this hook.
            # Reject ALL inner absolute-form requests; do not try to reconstruct
            # an original target from partially normalized fields.
            if request.authority:
                return
        elif (request.scheme, request.port) != ("http", 80):
            return
        hosts = request.headers.get_all("host")
        if len(hosts) != 1 or not self.authority_matches(hosts[0], request.host, request.port):
            return
        # One HTTP exchange per client/upstream connection in strict mode.
        # Numeric address pinning defeats the engine's hostname-keyed pooling.
        # Use ordinary HTTP closure, not private transport/connection mutations.
        request.headers["connection"] = "close"
        request.stream = True
        self.approved_targets.setdefault(f.client_conn.id, set()).add((request.host, request.port))
        f.error = None

    async def dial_address(self, host: str, port: int) -> tuple[str, int]:
        async with asyncio.timeout(3):
            answers = await asyncio.get_running_loop().getaddrinfo(
                host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
            )
        addresses = [ipaddress.IPv4Address(answer[4][0]) for answer in answers]
        if not addresses or any(
            not address.is_global
            or address.is_multicast
            or address in self.host_addresses
            or any(address in network for network in SPECIAL_DESTINATIONS)
            for address in addresses
        ):
            raise ValueError("destination denied")
        return str(addresses[0]), port

    async def server_connect(self, data):
        data.server.error = DENIED
        if not self.configuration_valid():
            return
        host, port = data.server.address
        if (host, port) not in self.approved_targets.get(data.client.id, set()):
            return
        data.server.address = await self.dial_address(host, port)
        if port == 443:
            # Numeric dialing must not change upstream SNI/hostname verification.
            data.server.sni = host
        data.server.error = None

    def responseheaders(self, f: http.HTTPFlow):
        f.error = flow.Error(DENIED)
        if not self.configuration_valid():
            return
        if f.response.status_code == 101 or not self.valid_headers(f.response):
            return
        # Explicitly tell clients to reconnect; don't advertise keep-alive then
        # silently close. Streaming ends normally before either socket closes.
        f.response.headers["connection"] = "close"
        f.response.stream = True
        f.error = None


class GuardedTLS(tlsconfig.TlsConfig):
    def __init__(self, policy):
        super().__init__()
        self.policy = policy
        self.valid_hello = set()

    def tls_clienthello(self, data):
        data.ignore_connection = False
        data.establish_server_tls_first = False
        self.valid_hello.discard(data.context.client.id)
        if not self.policy.configuration_valid():
            return
        expected = self.policy.tunnels.get(data.context.client.id)
        if expected is None or data.client_hello.sni != expected:
            return
        if any(kind == 0xFE0D for kind, _ in data.client_hello.extensions):
            return  # ECH cannot be verified by strict mode.
        self.valid_hello.add(data.context.client.id)

    def tls_start_client(self, data):
        data.ssl_conn = None
        if not self.policy.configuration_valid():
            return
        if data.context.client.id not in self.valid_hello:
            return  # The engine closes when no TLS context is provided.
        try:
            super().tls_start_client(data)
        except Exception:
            # Even a partially initialized SSL context must not survive failure.
            data.ssl_conn = None
            raise

    def tls_start_server(self, data):
        data.ssl_conn = None
        if not self.policy.configuration_valid():
            return
        try:
            super().tls_start_server(data)
        except BaseException:
            data.ssl_conn = None
            raise

    def client_disconnected(self, client):
        self.valid_hello.discard(client.id)


def make_proxy(
    allowed_domains: frozenset[str],
    confdir: Path,
    *,
    host_addresses: frozenset[str],
    listen_host="127.0.0.1",
    listen_port=0,
    upstream_ca=None,
):
    """Construct a fixed engine inventory; no scripts, UI, or option input."""
    opts = options.Options(
        listen_host=listen_host,
        listen_port=listen_port,
        confdir=str(confdir),
        mode=["regular"],
        http2=False,
        rawtcp=False,
        websocket=False,
        upstream_cert=False,
        ssl_insecure=False,
    )
    master = Master(opts, with_termlog=False)
    policy = Policy(allowed_domains, host_addresses)
    master.addons.add(
        proxyserver.Proxyserver(),
        DefaultDeny(),
        GuardedNextLayer(policy),
        GuardedTLS(policy),
        policy,
    )
    opts.update(
        connection_strategy="lazy", store_streamed_bodies=False, validate_inbound_headers=True
    )
    if upstream_ca is not None:
        opts.update(ssl_verify_upstream_trusted_ca=str(upstream_ca))
    policy.seal(master)
    return master, policy
