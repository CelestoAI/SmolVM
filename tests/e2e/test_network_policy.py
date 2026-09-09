"""Live policy tests on a disposable Linux runner, using controlled destinations."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest
from _util import BOOT_TIMEOUT, require_backend_available, selected_backend

from smolvm import SmolVM
from smolvm.exceptions import SmolVMError
from smolvm.host.network import NetworkManager
from smolvm.storage import MemoryStateManager
from smolvm.types import SnapshotType

pytestmark = pytest.mark.e2e


def privileged(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*([] if os.geteuid() == 0 else ["sudo", "-n"]), *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.fixture
def policy_lab(tmp_path: Path):
    """A routed namespace with two HTTP destinations and a UDP echo service."""
    if sys.platform != "linux":
        pytest.skip("Network policy packet tests require Linux")
    privileged("true")  # Fail, do not skip, if a selected Linux runner lacks privileges.
    suffix = secrets.token_hex(3)
    ns, host_if, peer_if = f"np-{suffix}", f"nph{suffix}", f"npp{suffix}"
    octet = secrets.randbelow(250) + 1
    prefix = f"198.18.{octet}"
    gateway, allowed, denied = f"{prefix}.1", f"{prefix}.2", f"{prefix}.3"
    log = tmp_path / "requests.jsonl"
    server = tmp_path / "server.py"
    server.write_text("""import http.server, json, socket, threading
from pathlib import Path
import sys
log = Path(sys.argv[1])
def record(value):
    with log.open("a") as f: f.write(json.dumps(value) + "\\n")
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        record(self.path)
        self.send_response(200); self.end_headers(); self.wfile.write(b"policy-ok")
    def log_message(self, *args): pass
def udp():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 53))
    while True:
        data, addr = sock.recvfrom(4096)
        record(data.decode(errors="replace")); sock.sendto(data, addr)
class V6Server(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6
def serve6(): V6Server(("::", 18082), Handler).serve_forever()
threading.Thread(target=serve6, daemon=True).start()
threading.Thread(target=udp, daemon=True).start()
http.server.ThreadingHTTPServer(("0.0.0.0", 18080), Handler).serve_forever()
""")
    process = None
    forwarding_rules: list[tuple[str, str]] = []
    ipv6_forwarding = privileged("sysctl", "-n", "net.ipv6.conf.all.forwarding").stdout.strip()
    try:
        privileged("sysctl", "-w", "net.ipv6.conf.all.forwarding=1")
        privileged("ip", "netns", "add", ns)
        privileged("ip", "link", "add", host_if, "type", "veth", "peer", "name", peer_if)
        privileged("ip", "link", "set", peer_if, "netns", ns)
        privileged("ip", "addr", "add", f"{gateway}/24", "dev", host_if)
        privileged("ip", "link", "set", host_if, "up")
        # Docker runners can default FORWARD to DROP. Permit only this lab's
        # interface through that ambient firewall; SmolVM's earlier policy
        # chains still decide which guest packets may reach it.
        for binary in ("iptables", "ip6tables"):
            for direction in ("-i", "-o"):
                privileged(binary, "-w", "-I", "FORWARD", direction, host_if, "-j", "ACCEPT")
                forwarding_rules.append((binary, direction))
        privileged("sysctl", "-w", f"net.ipv6.conf.{host_if}.forwarding=1")
        privileged("ip", "-6", "addr", "add", "fd00:534d:1::1/64", "dev", host_if, "nodad")
        for address in (allowed, denied):
            privileged("ip", "-n", ns, "addr", "add", f"{address}/24", "dev", peer_if)
        privileged("ip", "-n", ns, "addr", "add", "169.254.111.222/32", "dev", peer_if)
        privileged("ip", "route", "add", "169.254.111.222/32", "dev", host_if)
        privileged("ip", "-n", ns, "link", "set", peer_if, "up")
        privileged(
            "ip", "-n", ns, "-6", "addr", "add", "fd00:534d:1::2/64", "dev", peer_if, "nodad"
        )
        privileged("ip", "-n", ns, "-6", "route", "add", "default", "via", "fd00:534d:1::1")
        privileged("ip", "-n", ns, "link", "set", "lo", "up")
        privileged("ip", "-n", ns, "route", "add", "default", "via", gateway)
        process = subprocess.Popen(
            [
                *([] if os.geteuid() == 0 else ["sudo", "-n"]),
                "ip",
                "netns",
                "exec",
                ns,
                sys.executable,
                str(server),
                str(log),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        import urllib.request

        for _ in range(50):
            try:
                with urllib.request.urlopen(
                    f"http://{allowed}:18080/ready", timeout=0.2
                ) as response:
                    assert response.read() == b"policy-ok"
                break
            except OSError:
                if process.poll() is not None:
                    pytest.fail(f"Policy responder failed: {process.stderr.read().decode()}")
                time.sleep(0.1)
        else:
            pytest.fail("Policy responder did not become ready")
        # This is a simulated metadata service entirely inside our namespace.
        with urllib.request.urlopen("http://169.254.111.222:18080/ready", timeout=1) as response:
            assert response.read() == b"policy-ok"
        yield allowed, denied, gateway, log, host_if
    finally:
        if process is not None:
            # Kill namespace-owned processes before deleting the namespace.
            result = privileged("ip", "netns", "pids", ns, check=False)
            for pid in result.stdout.split():
                privileged("kill", pid, check=False)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
        for binary, direction in reversed(forwarding_rules):
            privileged(
                binary, "-w", "-D", "FORWARD", direction, host_if, "-j", "ACCEPT", check=False
            )
        privileged("ip", "link", "del", host_if, check=False)
        privileged("ip", "netns", "del", ns, check=False)
        privileged("sysctl", "-w", f"net.ipv6.conf.all.forwarding={ipv6_forwarding}")


def test_firewall_packet_contract(policy_lab):
    """Exercise TCP/UDP, host access, reuse and IPv6 without KVM."""
    allowed, denied, gateway, log, host_if = policy_lab
    suffix = secrets.token_hex(3)
    ns = f"npg-{suffix}"
    tap = f"tap{secrets.randbelow(1000000) + 100000}"
    peer = f"g{suffix}"
    nm = NetworkManager()
    # A test-only point-to-point subnet; no allocated SmolVM addresses touched.
    local, guest = "192.0.2.1", "192.0.2.2"
    probe = """import socket,sys
family = socket.AF_INET6 if ":" in sys.argv[1] else socket.AF_INET
s=socket.socket(family, socket.SOCK_DGRAM if sys.argv[2]=="udp" else socket.SOCK_STREAM)
s.settimeout(1.5)
if sys.argv[2]=="udp": s.bind(("0.0.0.0", 19001))
try:
 port = 53 if sys.argv[2]=="udp" else (18082 if family==socket.AF_INET6 else 18080)
 s.connect((sys.argv[1], port))
 request = sys.argv[3] if sys.argv[2]=="udp" else "GET /"+sys.argv[3]+" HTTP/1.0\\r\\n\\r\\n"
 s.send(request.encode())
 assert s.recv(4096)
except (OSError,AssertionError): sys.exit(1)
"""

    def reachable(address, protocol="tcp", token="probe"):
        return (
            privileged(
                "ip",
                "netns",
                "exec",
                ns,
                sys.executable,
                "-c",
                probe,
                address,
                protocol,
                token,
                check=False,
            ).returncode
            == 0
        )

    try:
        privileged("ip", "netns", "add", ns)
        privileged("ip", "link", "add", tap, "type", "veth", "peer", "name", peer)
        privileged("ip", "link", "set", peer, "netns", ns)
        privileged("ip", "addr", "add", f"{local}/30", "dev", tap)
        privileged("ip", "link", "set", tap, "up")
        privileged("sysctl", "-w", f"net.ipv6.conf.{tap}.forwarding=1")
        privileged("ip", "-n", ns, "addr", "add", f"{guest}/30", "dev", peer)
        privileged("ip", "-n", ns, "link", "set", peer, "up")
        privileged("ip", "-n", ns, "route", "add", "default", "via", local)
        privileged("ip", "-6", "addr", "add", "fd00:534d:2::1/64", "dev", tap, "nodad")
        privileged("ip", "-n", ns, "-6", "addr", "add", "fd00:534d:2::2/64", "dev", peer, "nodad")
        privileged("ip", "-n", ns, "-6", "route", "add", "default", "via", "fd00:534d:2::1")
        nm.apply_network_policy(tap, None)
        nm.setup_nat(tap)
        assert reachable(allowed, token="open"), privileged("nft", "list", "ruleset").stdout
        assert reachable("fd00:534d:1::2", token="ipv6-open"), "\n".join(
            (
                privileged("nft", "list", "ruleset").stdout,
                privileged("ip", "-6", "neigh").stdout,
                privileged("ip", "-n", ns, "-6", "neigh").stdout,
                privileged("ip", "-6", "route").stdout,
                privileged("ip", "-n", ns, "-6", "route").stdout,
                privileged("sysctl", "-n", "net.ipv6.conf.all.forwarding").stdout,
            )
        )
        assert not reachable("169.254.111.222", token="metadata-denied")
        assert reachable(allowed, "udp", "dns-open")
        # Temporarily model another sandbox's routed interface, then restore it.
        neighbor_tap = f"tap{secrets.randbelow(1000000) + 2000000}"
        privileged("ip", "link", "set", host_if, "name", neighbor_tap)
        try:
            # Keep the ambient firewall permissive for the renamed lab too,
            # so only SmolVM's isolation rule can make this probe fail.
            for direction in ("-i", "-o"):
                privileged(
                    "iptables", "-w", "-I", "FORWARD", direction, neighbor_tap, "-j", "ACCEPT"
                )
            assert not reachable(allowed, token="neighbor-denied")
        finally:
            for direction in ("-i", "-o"):
                privileged(
                    "iptables",
                    "-w",
                    "-D",
                    "FORWARD",
                    direction,
                    neighbor_tap,
                    "-j",
                    "ACCEPT",
                    check=False,
                )
            privileged("ip", "link", "set", neighbor_tap, "name", host_if)
        assert reachable(allowed, token="neighbor-restored")
        nm.apply_network_policy(tap, [allowed])
        assert reachable(allowed, token="allowed")
        assert not reachable("fd00:534d:1::2", token="ipv6-restricted")
        assert reachable(allowed, "udp", "dns-allowed")
        assert not reachable(denied, token="denied")
        # A guest changing its own source address cannot escape the interface policy.
        privileged("ip", "route", "add", "192.0.2.4/32", "dev", tap)
        privileged("ip", "-n", ns, "addr", "add", "192.0.2.4/32", "dev", peer)
        privileged("ip", "-n", ns, "route", "replace", "default", "via", local, "src", "192.0.2.4")
        assert reachable(allowed, token="spoof-allowed")
        assert not reachable(denied, token="spoof-denied")
        privileged("ip", "-n", ns, "route", "replace", "default", "via", local, "src", guest)
        assert not reachable(denied, "udp", "dns-denied")
        # Invalid replacement must leave the old policy intact, not flush it.
        with pytest.raises(SmolVMError):
            nm._run_nft_script(nm._network_policy_script(tap, None) + "invalid nft syntax\n")
        assert not reachable(denied, token="failed-update")
        assert reachable(allowed, token="old-policy")
        nm.apply_network_policy(tap, [])
        # Even an accidental subsequent blanket NAT permission cannot bypass off.
        nm.setup_nat(tap)
        assert not reachable(allowed, token="off")
        assert not reachable("fd00:534d:1::2", token="ipv6-off")
        assert not reachable(allowed, "udp", "dns-off")
        # Host-addressed probes must hit the input drop, independently of a listener.
        assert not reachable(local, token="host-off")
        privileged("ip", "addr", "add", "2001:db8:123::1/64", "dev", tap, "nodad")
        privileged("ip", "-n", ns, "addr", "add", "2001:db8:123::2/64", "dev", peer, "nodad")
        assert not reachable("2001:db8:123::1", token="ipv6-off")
        # Counters prove the IPv6/input probes reached policy, not a missing route.
        rules = privileged("nft", "-j", "list", "table", "inet", nm._policy_table(tap)).stdout
        counters = [
            expr["counter"]["packets"]
            for item in json.loads(rules)["nftables"]
            for expr in item.get("rule", {}).get("expr", [])
            if "counter" in expr and item.get("rule", {}).get("chain") == "input"
        ]
        assert sum(counters) > 0
        nm.remove_network_policy(tap)
        nm.apply_network_policy(tap, [denied])
        assert reachable(denied, token="reused")
        assert not reachable(allowed, token="stale")
        requests = log.read_text()
        for blocked in (
            '"/denied"',
            '"dns-denied"',
            '"/off"',
            '"dns-off"',
            '"/stale"',
            '"/neighbor-denied"',
            '"/failed-update"',
            '"/ipv6-restricted"',
            '"/ipv6-off"',
        ):
            assert blocked not in requests
    finally:
        privileged("ip", "link", "del", tap, check=False)
        with suppress(Exception):
            nm.remove_network_policy(tap)
        with suppress(Exception):
            nm.cleanup_nat_rules(tap)
        privileged("ip", "netns", "del", ns, check=False)


@pytest.mark.parametrize("mode", ["off", "restricted"])
def test_firecracker_policy_lifecycle(policy_lab, request, tmp_path, mode):
    if selected_backend(request.config) == "qemu":
        pytest.skip("Explicit policy currently ships on Firecracker only")
    require_backend_available("firecracker", request.config, sandbox_name="network-policy")
    allowed, denied, gateway, log, host_if = policy_lab
    settings = {"mode": mode}
    if mode == "restricted":
        settings["allowed_cidrs"] = [allowed]
    inventory = MemoryStateManager(tmp_path / "inventory")
    sandbox = SmolVM(
        backend="firecracker",
        os="alpine",
        comm_channel="vsock",
        internet_settings=settings,
        state_manager=inventory,
    )
    restored = None
    snapshot = None
    try:
        sandbox.start(boot_timeout=BOOT_TIMEOUT)
        payload = tmp_path / "payload"
        payload.write_bytes(b"network-off-file-transfer")
        sandbox.upload_file(str(payload), "/tmp/policy-payload")
        output = tmp_path / "downloaded"
        sandbox.download_file("/tmp/policy-payload", str(output))
        assert output.read_bytes() == payload.read_bytes()

        def check(vm):
            vm.upload_file(str(payload), "/tmp/policy-payload")
            vm.download_file("/tmp/policy-payload", str(output))
            assert output.read_bytes() == payload.read_bytes()
            assert vm.run("printf control-ok").stdout == "control-ok"
            result = vm.run(f"wget -T 1 -qO- http://{allowed}:18080/vm-allowed", timeout=5)
            assert (result.exit_code == 0) is (mode == "restricted")
            assert (
                vm.run(f"wget -T 1 -qO- http://{denied}:18080/vm-denied", timeout=5).exit_code != 0
            )

        check(sandbox)
        sandbox.stop()
        sandbox.start(boot_timeout=BOOT_TIMEOUT)
        check(sandbox)
        snapshot = sandbox.snapshot(snapshot_type=SnapshotType.DISK)
        sandbox.stop()
        sandbox.delete()
        restored = SmolVM.from_snapshot(
            snapshot.snapshot_id, backend="firecracker", resume_vm=True, state_manager=inventory
        )
        check(restored)
        assert '"/vm-denied"' not in log.read_text()
    finally:
        target = restored or sandbox
        with suppress(Exception):
            target.stop()
        with suppress(Exception):
            target.delete()
        if snapshot is not None:
            with suppress(Exception):
                target._sdk.delete_snapshot(snapshot.snapshot_id)
