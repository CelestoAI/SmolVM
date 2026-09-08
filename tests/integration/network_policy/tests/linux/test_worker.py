"""Process/credential tests without booting QEMU; no guest isolation claims."""

import json
import os
import subprocess
import sys

import pytest
from network_policy import NetworkPolicy
from network_policy.firewall import NetworkBinding
from network_policy.process import ManagedProxy

pytestmark = pytest.mark.skipif(
    os.environ.get("SMOLVM_POLICY_LINUX_TESTS") != "1",
    reason="requires the disposable Linux test container",
)


def nft(*args):
    return subprocess.run(["nft", *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def admission():
    bindings = [
        NetworkBinding(
            tap=f"worker-tap{index}",
            guest_ip=f"127.0.0.{index + 2}",
            gateway_ip="127.0.0.1",
            host_addresses=("127.0.0.1",),
            proxy_uid=65534 - index,
            proxy_port=18080 + index,
        )
        for index in range(2)
    ]
    for binding in bindings:
        binding.install()
    try:
        yield bindings
    finally:
        for binding in bindings:
            subprocess.run(["nft", "delete", "table", "inet", binding.table], capture_output=True)


def proxy(bindings, python=sys.executable):
    return ManagedProxy(
        python,
        policy=NetworkPolicy(allowed_domains=["allowed.example"]),
        binding=bindings[0],
    )


def test_failed_start_fences_only_its_admission_and_cleans_up(admission):
    for binding in admission:
        binding.admit()
    worker = proxy(admission, "/usr/bin/false")
    try:
        with pytest.raises((RuntimeError, BrokenPipeError)):
            worker.start()
        assert "18080" not in nft("list", "set", "inet", admission[0].table, "admitted_ports")
        assert "18081" in nft("list", "set", "inet", admission[1].table, "admitted_ports")
        assert worker.process.poll() is not None
    finally:
        worker.close()
    assert not worker.directory.exists()


def test_worker_inherits_neither_secret_environment_nor_open_descriptor(
    admission, tmp_path, monkeypatch
):
    monkeypatch.setenv("SYNTHETIC_HOST_SECRET", "must-not-cross-launch")
    secret = tmp_path / "inherited-secret"
    secret.write_text("not a real credential")
    worker = proxy(admission)
    with secret.open("rb") as inherited:
        os.set_inheritable(inherited.fileno(), True)
        try:
            worker.start()
            script = (
                "import json,os,pathlib,sys; p=pathlib.Path('/proc')/sys.argv[1]; "
                "print(json.dumps({'env':(p/'environ').read_bytes().decode(),"
                "'fds':{f.name:os.readlink(f) for f in (p/'fd').iterdir()}}))"
            )
            # Read kernel process state as the worker's UID, not a worker self-report.
            result = subprocess.run(
                [
                    "/usr/bin/setpriv",
                    "--reuid=65534",
                    "--regid=65534",
                    "--clear-groups",
                    "--inh-caps=-all",
                    "--ambient-caps=-all",
                    "--bounding-set=-all",
                    "--no-new-privs",
                    sys.executable,
                    "-c",
                    script,
                    str(worker.process.pid),
                ],
                check=True,
                capture_output=True,
                text=True,
                env={},
                timeout=5,
            )
            actual = json.loads(result.stdout)
            assert "SYNTHETIC_HOST_SECRET" not in actual["env"]
            assert "must-not-cross-launch" not in actual["env"]
            assert str(secret) not in actual["fds"].values()
            assert actual["fds"]["2"] == "/dev/null"
            assert len(actual["fds"]) < 32
        finally:
            worker.close()
    assert not worker.directory.exists()


def test_fence_failure_still_stops_worker_and_reports_failure(admission):
    worker = proxy(admission)
    try:
        worker.start()
        nft("delete", "table", "inet", admission[0].table)
        with pytest.raises(RuntimeError, match="did not verify fencing"):
            worker.close()
        assert worker.process.poll() is not None
        assert not worker.directory.exists()
    finally:
        if worker.process.poll() is None:
            worker.process.kill()
            worker.process.wait(timeout=5)


def test_start_cannot_replace_live_worker_and_close_is_idempotent(admission):
    worker = proxy(admission)
    try:
        worker.start()
        pid = worker.process.pid
        with pytest.raises(RuntimeError):
            worker.start()
        assert worker.process.pid == pid
        assert worker.process.poll() is None
        assert "18080" in nft("list", "set", "inet", admission[0].table, "admitted_ports")
    finally:
        worker.close()
    worker.close()
    with pytest.raises(RuntimeError):
        worker.start()


def test_concurrent_cleanup_and_unstarted_close(admission):
    from concurrent.futures import ThreadPoolExecutor

    worker = proxy(admission)
    try:
        worker.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker.close) for _ in range(2)]
            for future in futures:
                future.result(timeout=15)
        assert worker.process.poll() is not None
        assert not worker.directory.exists()
    finally:
        worker.close()
    unstarted = proxy(admission)
    unstarted.close()
    unstarted.close()
    with pytest.raises(RuntimeError):
        unstarted.start()


def test_maximum_domain_configuration_crosses_worker_boundary(admission):
    domains = [
        ".".join((f"{index:03d}" + "a" * 60, "b" * 63, "c" * 63, "d" * 61)) for index in range(100)
    ]
    policy = NetworkPolicy(allowed_domains=domains)
    assert len(json.dumps(policy.model_dump(mode="json"))) > 16384
    worker = ManagedProxy(sys.executable, policy=policy, binding=admission[0])
    try:
        worker.start()
        assert worker.public_ca.startswith(b"-----BEGIN CERTIFICATE-----")
    finally:
        worker.close()
