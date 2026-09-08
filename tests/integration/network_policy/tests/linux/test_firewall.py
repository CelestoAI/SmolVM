"""Real kernel packets over TAP, only in an explicitly opted-in container.

This is a firewall fixture, NOT a QEMU guest or a production rule installer.
It never flushes shared rules, uses no host mounts/network, and tests only its
own network namespace. Socket probes have synthetic numeric destinations.
"""

import fcntl
import json
import os
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from network_policy.firewall import NetworkBinding

pytestmark = pytest.mark.skipif(
    os.environ.get("SMOLVM_POLICY_LINUX_TESTS") != "1",
    reason="requires the disposable Linux TAP test container",
)


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 65535) + (total >> 16)
    return (~total) & 65535


def syn(src, dst, sport, dport, source_mac, dest_mac):
    source = socket.inet_aton(src)
    destination = socket.inet_aton(dst)
    tcp = struct.pack("!HHIIBBHHH", sport, dport, 12345, 0, 0x50, 2, 4096, 0, 0)
    pseudo = source + destination + struct.pack("!BBH", 0, 6, len(tcp))
    tcp = tcp[:16] + struct.pack("!H", checksum(pseudo + tcp)) + tcp[18:]
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 40, 1, 0, 64, 6, 0, source, destination)
    ip = ip[:10] + struct.pack("!H", checksum(ip)) + ip[12:]
    return dest_mac + source_mac + b"\x08\x00" + ip + tcp


def counter(name):
    data = json.loads(run("nft", "-j", "list", "counters"))
    return sum(
        item["counter"]["packets"]
        for item in data["nftables"]
        if "counter" in item
        and item["counter"]["name"] == name
        and item["counter"]["table"].startswith("smolvm_np_")
    )


@pytest.fixture(scope="module")
def network():
    if os.geteuid() != 0 or not Path("/dev/net/tun").exists():
        pytest.fail("run using Dockerfile.linux and its documented container flags")
    taps = []
    children = []
    bindings = []
    host_listener = socket.socket()
    stop_renewal = threading.Event()
    renewal = None
    try:
        host_listener.bind(("0.0.0.0", 80))
        host_listener.listen(8)
        for index, gateway, guest in [(0, "10.0.0.1", "10.0.0.2"), (1, "10.0.0.5", "10.0.0.6")]:
            name = f"spike-tap{index}"
            fd = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK)
            taps.append((fd, name, gateway, guest))
            fcntl.ioctl(fd, 0x400454CA, struct.pack("16sH", name.encode(), 0x1002))
            run("ip", "addr", "add", f"{gateway}/30", "dev", name)
            run("ip", "link", "set", name, "up")
            run(
                "ip",
                "neigh",
                "replace",
                guest,
                "lladdr",
                "02:00:00:00:00:02",
                "nud",
                "permanent",
                "dev",
                name,
            )
        run("ip", "addr", "add", "8.8.4.4/32", "dev", "lo")
        run("ip", "route", "add", "default", "via", "10.0.0.6", "dev", "spike-tap1")
        for index, (_, name, gateway, guest) in enumerate(taps):
            binding = NetworkBinding(
                tap=name,
                guest_ip=guest,
                gateway_ip=gateway,
                host_addresses=("127.0.0.1", "10.0.0.1", "10.0.0.5", "8.8.4.4"),
                resolver_addresses=("10.0.0.6",),
                proxy_uid=65534 - index,
                proxy_port=18080 + index,
                platform_ports=(8444,),
            )
            bindings.append(binding)
            binding.install()
            binding.admit()
        # Deliberately permissive earlier chains cannot defeat another base
        # chain's drop. This models the existing blanket accepts we must survive.
        run("nft", "add", "table", "inet", "spike_blanket_accept")
        for hook in ("input", "forward", "output"):
            run(
                "nft",
                "add",
                "chain",
                "inet",
                "spike_blanket_accept",
                hook,
                f"{{ type filter hook {hook} priority -200; policy accept; }}",
            )
        for gateway, port, uid in [("10.0.0.1", 18080, 65534), ("10.0.0.5", 18081, 65533)]:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "import socket,sys; s=socket.socket(); "
                    "s.bind((sys.argv[1],int(sys.argv[2]))); s.listen(32); "
                    "print('ready'); sys.stdin.buffer.read(1)",
                    gateway,
                    str(port),
                ],
                user=uid,
                group=uid,
                extra_groups=(),
                env={},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            children.append(child)
            assert select.select([child.stdout], [], [], 5)[0], "listener startup timed out"
            assert child.stdout.readline() == b"ready\n"
            status = dict(
                line.split(":", 1)
                for line in Path(f"/proc/{child.pid}/status").read_text().splitlines()
            )
            assert status["Uid"].split() == [str(uid)] * 4
            assert status["NoNewPrivs"].strip() == "1"
            for capability in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
                assert int(status[capability].strip(), 16) == 0

        def renew_leases():
            active = list(bindings)
            while not stop_renewal.wait(0.5):
                for binding in active[:]:
                    try:
                        binding.renew_admission()
                    except subprocess.CalledProcessError:
                        # A test deliberately fenced/rebuilt this table. Never
                        # readmit it, and continue serving the independent VM.
                        active.remove(binding)

        renewal = threading.Thread(target=renew_leases, daemon=True)
        renewal.start()
        yield taps
    finally:
        stop_renewal.set()
        if renewal is not None:
            renewal.join(timeout=6)
            assert not renewal.is_alive()
        for child in children:
            child.communicate(input=b"", timeout=5)
        host_listener.close()
        for binding in bindings:
            subprocess.run(["nft", "delete", "table", "inet", binding.table], capture_output=True)
        subprocess.run(
            ["nft", "delete", "table", "inet", "spike_blanket_accept"], capture_output=True
        )
        for fd, _, _, _ in taps:
            os.close(fd)  # Nonpersistent TAP devices/rules are disposable.


def inject(tap, src, dst, dport, sport):
    fd, name, _, _ = tap
    mac = bytes.fromhex(Path(f"/sys/class/net/{name}/address").read_text().strip().replace(":", ""))
    os.write(fd, syn(src, dst, sport, dport, bytes.fromhex("020000000002"), mac))


def has_synack(tap, sport, timeout=0.3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not select.select([tap[0]], [], [], max(0, deadline - time.monotonic()))[0]:
            break
        frame = os.read(tap[0], 65536)
        if len(frame) < 54 or frame[12:14] != b"\x08\x00" or frame[23] != 6:
            continue
        tcp = 14 + (frame[14] & 15) * 4
        if struct.unpack("!H", frame[tcp + 2 : tcp + 4])[0] == sport:
            return frame[tcp + 13] & 0x12 == 0x12
    return False


@pytest.mark.parametrize("index", [0, 1])
def test_guest_reaches_only_its_assigned_proxy(network, index):
    tap = network[index]
    before = counter("guest_drops")
    inject(tap, tap[3], tap[2], 18080 + index, 30000 + index)
    assert has_synack(tap, 30000 + index)
    assert counter("guest_drops") == before


@pytest.mark.parametrize(
    "source,destination,port",
    [
        ("10.0.0.6", "10.0.0.1", 18080),  # Spoof another guest on TAP0.
        ("10.0.0.2", "10.0.0.5", 18081),  # Other proxy listener.
        ("10.0.0.2", "10.0.0.1", 22),  # Host service.
        ("10.0.0.2", "8.8.4.4", 80),  # Public address of this host.
        ("10.0.0.2", "8.8.8.8", 443),  # Direct Internet traffic.
        ("10.0.0.2", "169.254.169.254", 80),  # Metadata.
        ("10.0.0.2", "10.0.0.6", 80),  # Another TAP guest.
        ("10.0.0.2", "8.8.8.8", 53),  # Alternate DNS resolver.
    ],
)
def test_guest_new_flows_are_dropped_before_host_or_forwarding(network, source, destination, port):
    before = counter("guest_drops")
    while select.select([network[1][0]], [], [], 0)[0]:
        os.read(network[1][0], 65536)
    inject(network[0], source, destination, port, 31000 + port)
    assert not has_synack(network[0], 31000 + port)
    assert not outbound_seen(network[1][0], destination, port, False)
    assert counter("guest_drops") > before


@pytest.mark.parametrize(
    "address,port,udp,allowed",
    [
        ("8.8.8.8", 80, False, True),
        ("8.8.8.8", 443, False, True),
        ("10.0.0.6", 53, True, True),
        ("10.0.0.6", 53, False, True),
        ("127.0.0.1", 80, False, False),
        ("10.0.0.1", 18080, False, False),
        ("169.254.169.254", 80, False, False),
        ("8.8.4.4", 80, False, False),
        ("10.0.0.6", 443, False, False),
        ("192.0.0.9", 443, False, False),
        ("8.8.8.8", 53, True, False),
        ("8.8.8.8", 443, True, False),
        ("8.8.8.8", 22, False, False),
    ],
)
def test_proxy_identity_has_independent_output_restrictions(network, address, port, udp, allowed):
    name = "proxy_allowed" if allowed else "proxy_drops"
    before = counter(name)
    # Drain previous SYN/ARP replies before observing this probe's packets.
    while select.select([network[1][0]], [], [], 0)[0]:
        os.read(network[1][0], 65536)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket,sys\n"
            "s=socket.socket(socket.AF_INET,int(sys.argv[3])); s.settimeout(.15)\n"
            "try:\n"
            " print(s.connect_ex((sys.argv[1],int(sys.argv[2]))))\n"
            " if int(sys.argv[3]) == socket.SOCK_DGRAM: s.send(b'x')\n"
            "except OSError: pass\n"
            "finally: s.close()\n",
            address,
            str(port),
            str(socket.SOCK_DGRAM if udp else socket.SOCK_STREAM),
        ],
        user=65534,
        group=65534,
        extra_groups=(),
        env={},
        check=True,
        capture_output=True,
        timeout=3,
    )
    assert counter(name) > before
    assert outbound_seen(network[1][0], address, port, udp) is allowed
    if address in {"127.0.0.1", "8.8.4.4", "10.0.0.1"}:
        assert result.stdout.strip() != b"0", "proxy reached the real host listener"


def outbound_seen(fd, address, port, udp):
    deadline = time.monotonic() + 0.1
    while time.monotonic() < deadline:
        if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
            return False
        frame = os.read(fd, 65536)
        if len(frame) < 42 or frame[12:14] != b"\x08\x00":
            continue
        if frame[23] != (17 if udp else 6) or frame[30:34] != socket.inet_aton(address):
            continue
        transport = 14 + (frame[14] & 15) * 4
        if struct.unpack("!H", frame[transport + 2 : transport + 4])[0] == port:
            return True
    return False


@pytest.mark.parametrize("port", [53, 443])
def test_guest_udp_cannot_escape(network, port):
    tap = network[0]
    source = socket.inet_aton(tap[3])
    destination = socket.inet_aton("8.8.8.8")
    udp = struct.pack("!HHHH", 32000, port, 9, 0) + b"x"
    pseudo = source + destination + struct.pack("!BBH", 0, 17, len(udp))
    udp = udp[:6] + struct.pack("!H", checksum(pseudo + udp) or 65535) + udp[8:]
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 29, 2, 0, 64, 17, 0, source, destination)
    ip = ip[:10] + struct.pack("!H", checksum(ip)) + ip[12:]
    mac = bytes.fromhex(
        Path(f"/sys/class/net/{tap[1]}/address").read_text().strip().replace(":", "")
    )
    while select.select([network[1][0]], [], [], 0)[0]:
        os.read(network[1][0], 65536)
    before = counter("guest_drops")
    os.write(tap[0], mac + bytes.fromhex("020000000002") + b"\x08\x00" + ip + udp)
    assert not outbound_seen(network[1][0], "8.8.8.8", port, True)
    assert counter("guest_drops") > before


def test_guest_ipv6_is_dropped(network):
    tap = network[0]
    run("ip", "-6", "addr", "add", "fd00::1/64", "dev", tap[1], "nodad")
    source = socket.inet_pton(socket.AF_INET6, "fd00::2")
    destination = socket.inet_pton(socket.AF_INET6, "fd00::1")
    tcp = struct.pack("!HHIIBBHHH", 33000, 18080, 1, 0, 0x50, 2, 4096, 0, 0)
    pseudo = source + destination + struct.pack("!I3xB", len(tcp), 6)
    tcp = tcp[:16] + struct.pack("!H", checksum(pseudo + tcp)) + tcp[18:]
    ipv6 = struct.pack("!IHBB16s16s", 6 << 28, len(tcp), 6, 64, source, destination)
    mac = bytes.fromhex(
        Path(f"/sys/class/net/{tap[1]}/address").read_text().strip().replace(":", "")
    )
    before = counter("guest_drops")
    os.write(tap[0], mac + bytes.fromhex("020000000002") + b"\x86\xdd" + ipv6 + tcp)
    deadline = time.monotonic() + 1
    while counter("guest_drops") == before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert counter("guest_drops") > before


def test_rebuild_closes_admission_without_disturbing_other_vm(network):
    first = NetworkBinding(
        tap=network[0][1],
        guest_ip=network[0][3],
        gateway_ip=network[0][2],
        host_addresses=("127.0.0.1", "10.0.0.1", "10.0.0.5", "8.8.4.4"),
        resolver_addresses=("10.0.0.6",),
        proxy_uid=65534,
        proxy_port=18080,
        platform_ports=(8444,),
    )
    try:
        first.install()
        inject(network[0], "10.0.0.2", "10.0.0.1", 18080, 39001)
        assert not has_synack(network[0], 39001)
        inject(network[1], "10.0.0.6", "10.0.0.5", 18081, 39002)
        assert has_synack(network[1], 39002)
    finally:
        first.admit()
    inject(network[0], "10.0.0.2", "10.0.0.1", 18080, 39003)
    assert has_synack(network[0], 39003)


def test_empty_policy_does_not_leave_proxy_admission(network):
    empty = NetworkBinding(
        tap=network[0][1],
        guest_ip=network[0][3],
        gateway_ip=network[0][2],
        host_addresses=("127.0.0.1", "10.0.0.1", "10.0.0.5", "8.8.4.4"),
    )
    try:
        empty.install()
        inject(network[0], "10.0.0.2", "10.0.0.1", 18080, 39101)
        assert not has_synack(network[0], 39101)
        inject(network[0], "10.0.0.2", "8.8.8.8", 443, 39102)
        assert not outbound_seen(network[1][0], "8.8.8.8", 443, False)
        with pytest.raises(ValueError, match="deny-all"):
            empty.admit()
    finally:
        # Restore fixture ownership; live policy updates are not a public API.
        original = NetworkBinding(
            tap=empty.tap,
            guest_ip=empty.guest_ip,
            gateway_ip=empty.gateway_ip,
            host_addresses=empty.host_addresses,
            resolver_addresses=("10.0.0.6",),
            proxy_uid=65534,
            proxy_port=18080,
            platform_ports=(8444,),
        )
        original.install()
        original.admit()


def test_new_host_address_is_blocked_without_inventory_refresh(network):
    # The root listener is already bound to every local address. This address
    # was absent when the policy was installed; kernel routing must still deny it.
    run("ip", "addr", "add", "9.9.9.9/32", "dev", "lo")
    before = counter("proxy_drops")
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import socket; s=socket.socket(); s.settimeout(.2); "
                "print(s.connect_ex(('9.9.9.9',80))); s.close()",
            ],
            user=65534,
            group=65534,
            extra_groups=(),
            env={},
            check=True,
            capture_output=True,
            timeout=3,
        )
        assert result.stdout.strip() != b"0"
        assert counter("proxy_drops") > before
    finally:
        run("ip", "addr", "del", "9.9.9.9/32", "dev", "lo")
