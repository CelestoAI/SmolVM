"""Real QEMU guests + managed proxy + kernel firewall, in a disposable container.

Guest A is hostile/root. Guest B is a synthetic public HTTPS origin on a
separate TAP. No destination validator, resolver, socket or TLS hook is mocked.
This is not the SmolVM/cloud provisioning or production init path.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from guest import Guest
from network_policy import NetworkPolicy
from network_policy.firewall import NetworkBinding
from network_policy.process import ManagedProxy

pytestmark = pytest.mark.skipif(
    os.environ.get("SMOLVM_POLICY_QEMU_TESTS") != "1",
    reason="requires the disposable QEMU test container",
)


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10).stdout


def request(
    guest, url="https://allowed.example/", *, direct=False, host=None, ca="/tmp/proxy-ca.pem"
):
    args = ["curl", "-sS", "--max-time", "3", "--cacert", ca]
    if direct:
        args += ["--noproxy", "*", "--resolve", "allowed.example:443:11.0.0.2"]
    else:
        args += ["--proxy", "http://10.0.0.1:18080"]
    if host:
        args += ["-H", f"Host: {host}"]
    args += [url]
    return guest.json(
        f"import subprocess,json; p=subprocess.run({args!r},capture_output=True); "
        "print('RESULT='+json.dumps({'code':p.returncode,'body':p.stdout.decode(errors='replace')}))"
    )


@pytest.fixture(scope="module")
def environment(tmp_path_factory):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from mitmproxy import certs

    root = tmp_path_factory.mktemp("combined")
    public = Path(tempfile.mkdtemp(prefix="smolvm-public-pki-"))
    public.chmod(0o755)
    origin = guest = proxy = None
    taps = []
    try:
        for name, address in [
            ("spike-tap0", "10.0.0.1/30"),
            ("spike-tap1", "10.0.0.5/30"),
            ("origin-tap", "11.0.0.1/30"),
        ]:
            run("ip", "tuntap", "add", "dev", name, "mode", "tap")
            taps.append(name)
            run("ip", "addr", "add", address, "dev", name)
            run("ip", "link", "set", name, "up")
        run("ip", "route", "add", "default", "via", "11.0.0.2", "dev", "origin-tap")
        inventory = [
            address["local"]
            for interface in json.loads(run("ip", "-j", "-4", "addr"))
            for address in interface.get("addr_info", [])
        ]
        binding = NetworkBinding(
            tap="spike-tap0",
            guest_ip="10.0.0.2",
            gateway_ip="10.0.0.1",
            host_addresses=tuple(inventory),
            proxy_uid=65534,
            proxy_port=18080,
            platform_ports=(8444,),
        )
        binding.install()

        # A real TLS origin in its own VM, not a loopback dial substitution.
        store = certs.CertStore.from_store(root / "origin-pki", "origin", 2048)
        entry = store.get_cert("allowed.example", [x509.DNSName("allowed.example")], None)
        origin_ca = (root / "origin-pki" / "origin-ca-cert.pem").read_bytes()
        (public / "origin-ca.pem").write_bytes(origin_ca)
        (public / "origin-ca.pem").chmod(0o644)
        server_pem = entry.cert.to_pem() + entry.privatekey.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        origin = Guest(root / "origin", "origin-tap", "11.0.0.2/30", "11.0.0.1")
        origin.write("/tmp/server.pem", server_pem)
        origin.write(
            "/tmp/origin.py",
            b"""import http.server,ssl,json
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def do_GET(self):
        with open('/tmp/requests.jsonl','a') as f:
            f.write(json.dumps([self.headers.get('Host'),self.path])+'\\n')
        self.send_response(200)
        self.send_header('Content-Length','2')
        self.end_headers()
        self.wfile.write(b'ok')
    def log_message(self,*args): pass
server=http.server.ThreadingHTTPServer(('0.0.0.0',443),Handler)
ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain('/tmp/server.pem')
server.socket=ctx.wrap_socket(server.socket,server_side=True)
server.serve_forever()
""",
        )
        origin.command("python3 /tmp/origin.py >/tmp/origin.log 2>&1 & true")
        deadline = time.monotonic() + 60
        while True:
            output = origin.command("ss -lnt; cat /tmp/origin.log")
            if ":443" in output:
                break
            if time.monotonic() >= deadline:
                pytest.fail(
                    output
                    + origin.command(
                        "ps -ef; wc -c /tmp/origin.py /tmp/server.pem; "
                        "cat /tmp/origin.py; cat /etc/hosts; cat /etc/resolv.conf"
                    )
                )
            time.sleep(0.2)

        # The guest boots AFTER default denial is installed, before admission.
        guest = Guest(root / "guest", "spike-tap0", "10.0.0.2/30", "10.0.0.1")
        assert guest.json("import os,json; print('RESULT='+json.dumps(os.getuid()))") == 0
        guest.write("/tmp/origin-ca.pem", origin_ca)
        assert request(guest, direct=True, ca="/tmp/origin-ca.pem")["code"] != 0
        assert request(guest)["code"] != 0
        inventory = [
            address["local"]
            for interface in json.loads(run("ip", "-j", "-4", "addr"))
            for address in interface.get("addr_info", [])
        ]

        def new_proxy():
            item = ManagedProxy(
                sys.executable,
                policy=NetworkPolicy(allowed_domains=["allowed.example"]),
                binding=binding,
                upstream_ca=str(public / "origin-ca.pem"),
            )
            try:
                item.start()
            except BaseException:
                item.close()
                raise
            return item

        proxy = new_proxy()
        guest.write("/tmp/proxy-ca.pem", proxy.public_ca)
        env = {
            "root": root,
            "guest": guest,
            "origin": origin,
            "proxy": proxy,
            "new_proxy": new_proxy,
            "measurements": {},
        }
        yield env
    finally:
        # Tests may replace the proxy/guest while exercising recovery.
        if "env" in locals():
            proxy, guest = env["proxy"], env["guest"]
        if guest:
            guest.close()
        if proxy:
            proxy.close()
        if origin:
            origin.close()
        if "binding" in locals():
            binding.remove()
        for name in reversed(taps):
            run("ip", "link", "del", name)
        shutil.rmtree(public)


def test_root_guest_allowed_https_and_hostname_attacks(environment):
    guest = environment["guest"]
    assert request(guest) == {"code": 0, "body": "ok"}
    assert request(guest, "https://denied.example/")["code"] != 0
    assert request(guest, host="denied.example")["code"] != 0
    assert request(guest, direct=True, ca="/tmp/origin-ca.pem")["code"] != 0
    observed = environment["origin"].json(
        "import json; data=[json.loads(x) for x in open('/tmp/requests.jsonl')]; "
        "print('RESULT='+json.dumps(data))"
    )
    assert observed == [["allowed.example", "/"]]


def test_worker_crash_stays_closed_and_restart_rotates_trust(environment):
    guest, old = environment["guest"], environment["proxy"]
    old_ca = old.public_ca
    old.process.kill()
    old.process.wait(timeout=5)
    deadline = time.monotonic() + 5
    while "18080" in run("nft", "list", "set", "inet", old.binding.table, "admitted_ports"):
        assert time.monotonic() < deadline, "crash did not fence guest admission"
        time.sleep(0.05)
    assert request(guest)["code"] != 0
    assert request(guest, direct=True, ca="/tmp/origin-ca.pem")["code"] != 0
    old.close()
    environment["proxy"] = None
    replacement = environment["new_proxy"]()
    environment["proxy"] = replacement
    assert replacement.public_ca != old_ca
    assert request(guest)["code"] != 0, "stale guest trust accepted a new CA"
    guest.write("/tmp/proxy-ca.pem", replacement.public_ca)
    assert request(guest) == {"code": 0, "body": "ok"}


def test_real_worker_privileges_keys_and_logs(environment):
    proxy = environment["proxy"]
    status = dict(
        line.split(":", 1)
        for line in Path(f"/proc/{proxy.process.pid}/status").read_text().splitlines()
    )
    assert status["Uid"].split() == ["65534"] * 4
    assert status["Gid"].split() == ["65534"] * 4
    assert status["Groups"].strip() == ""
    assert status["NoNewPrivs"].strip() == "1"
    for name in ("CapInh", "CapPrm", "CapEff", "CapAmb", "CapBnd"):
        assert int(status[name], 16) == 0
    limits = Path(f"/proc/{proxy.process.pid}/limits").read_text()
    assert next(line for line in limits.splitlines() if line.startswith("Max open files")).split()[
        3:5
    ] == ["256", "256"]
    assert next(
        line for line in limits.splitlines() if line.startswith("Max core file size")
    ).split()[4:6] == ["0", "0"]
    assert proxy.directory.stat().st_mode & 0o777 == 0o700
    private = proxy.directory / "mitmproxy-ca.pem"
    assert private.stat().st_uid == 65534
    assert private.stat().st_mode & 0o077 == 0
    result = subprocess.run(
        [
            "/usr/bin/setpriv",
            "--reuid=65533",
            "--regid=65533",
            "--clear-groups",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "--bounding-set=-all",
            "--no-new-privs",
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).read_bytes()",
            str(private),
        ],
        capture_output=True,
        env={},
        timeout=5,
    )
    assert result.returncode != 0
    assert b"PermissionError" in result.stderr
    assert b"PRIVATE KEY" not in proxy.public_ca
    # A malformed credential-bearing request must not create retained flow data.
    guest = environment["guest"]
    guest.python(
        "import socket; s=socket.create_connection(('10.0.0.1',18080),3); "
        "s.sendall(b'GET http://allowed.example/SYNTHETIC_SECRET HTTP/1.1\\r\\n"
        "Host: denied.example\\r\\nAuthorization: Bearer SYNTHETIC_SECRET\\r\\n\\r\\n'); "
        "s.recv(4096); s.close()"
    )
    import select

    assert not select.select([proxy.process.stdout], [], [], 0.2)[0]
    for path in proxy.directory.iterdir():
        assert path.is_file()
        assert path.name.startswith("mitmproxy-ca") or path.name == "mitmproxy-dhparam.pem"
        assert b"SYNTHETIC_SECRET" not in path.read_bytes()


def test_python_client_and_initial_resource_measurement(environment):
    import ssl
    import statistics
    import urllib.request

    proxy = environment["proxy"]
    guest = environment["guest"]
    result = guest.json(
        "import urllib.request,ssl,json; "
        "opener=urllib.request.build_opener(urllib.request.ProxyHandler({'https':'http://10.0.0.1:18080'}),"
        "urllib.request.HTTPSHandler(context=ssl.create_default_context("
        "cafile='/tmp/proxy-ca.pem'))); "
        "print('RESULT='+json.dumps(opener.open('https://allowed.example/python',timeout=10).read().decode()))",
        timeout=90,  # First stdlib/SSL imports are slow under TCG; network stays bounded to 10s.
    )
    assert result == "ok"
    latencies = {}
    for label, ca, settings in (
        ("direct", proxy.config["upstream_ca"], {}),
        ("proxy", None, {"https": "http://10.0.0.1:18080"}),
    ):
        context = (
            ssl.create_default_context(cafile=ca)
            if ca
            else ssl.create_default_context(cadata=proxy.public_ca.decode())
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(settings), urllib.request.HTTPSHandler(context=context)
        )
        elapsed = []
        for _ in range(5):
            start = time.monotonic()
            with opener.open("https://allowed.example/measure", timeout=10) as response:
                assert response.read() == b"ok"
            elapsed.append((time.monotonic() - start) * 1000)
        latencies[label + "_median_ms"] = round(statistics.median(elapsed), 2)
    status = dict(
        line.split(":", 1)
        for line in Path(f"/proc/{proxy.process.pid}/status").read_text().splitlines()
    )
    measurements = {
        **latencies,
        "worker_rss_kib": int(status["VmRSS"].split()[0]),
        "worker_peak_rss_kib": int(status["VmHWM"].split()[0]),
        "origin": "QEMU TCG ARM64; not a production capacity benchmark",
    }
    environment["measurements"] = measurements
    print("MEASUREMENTS=" + json.dumps(measurements, sort_keys=True))


def test_disk_restore_requires_fresh_trust_and_keeps_direct_egress_denied(environment):
    guest, old = environment["guest"], environment["proxy"]
    guest.write("/root/restore-marker", b"persisted workspace")
    guest.command("sync")
    old.close()  # Fences admission before stopping/replacing the VM.
    environment["proxy"] = None
    guest.close()
    environment["guest"] = None
    restored = Guest(
        environment["root"] / "restored",
        "spike-tap0",
        "10.0.0.2/30",
        "10.0.0.1",
        restore_disk=guest.disk,
    )
    environment["guest"] = restored
    assert "persisted workspace" in restored.command("cat /root/restore-marker")
    assert request(restored)["code"] != 0
    assert request(restored, direct=True, ca="/tmp/origin-ca.pem")["code"] != 0
    replacement = environment["new_proxy"]()
    environment["proxy"] = replacement
    assert request(restored)["code"] != 0, "restored stale CA unexpectedly trusted new proxy"
    restored.write("/tmp/proxy-ca.pem", replacement.public_ca)
    assert request(restored) == {"code": 0, "body": "ok"}
