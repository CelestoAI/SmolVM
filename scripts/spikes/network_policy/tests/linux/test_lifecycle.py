"""Persistent supervision with real Linux processes; the VM is a sleeping child."""

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from network_policy import NetworkPolicy
from network_policy.lifecycle import (
    PreparedPolicy,
    cleanup_stopped_policy,
    policy_status,
    process_identity,
)
from test_worker import admission as _admission

admission = _admission

pytestmark = pytest.mark.skipif(
    os.environ.get("SMOLVM_POLICY_LINUX_TESTS") != "1", reason="disposable Linux container only"
)


@pytest.fixture
def vm():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def finished(path):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if path.exists():
            state = json.loads(path.read_text())
            if (
                state["status"] in {"stopped", "failed"}
                and process_identity(state["supervisor_pid"]) is None
            ):
                return state
        time.sleep(0.05)
    pytest.fail("supervisor did not finish and release its worker")


def test_supervisor_survives_client_exit_and_stops_with_vm(admission, vm, tmp_path):
    path = tmp_path / "policy.json"
    policy = NetworkPolicy(allowed_domains=["allowed.example"])
    script = (
        "import json,sys; from pathlib import Path; "
        "from network_policy import NetworkPolicy; "
        "from network_policy.firewall import NetworkBinding; "
        "from network_policy.lifecycle import PreparedPolicy; "
        "p=PreparedPolicy(NetworkPolicy(allowed_domains=['allowed.example']),"
        "NetworkBinding(**json.loads(sys.argv[1])),Path(sys.argv[2])); p.attach(int(sys.argv[3]))"
    )
    subprocess.run(
        [sys.executable, "-c", script, json.dumps(asdict(admission[0])), str(path), str(vm.pid)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert policy_status(path, policy, admission[0], vm.pid) is not None
    vm.terminate()
    vm.wait(timeout=5)
    state = finished(path)
    assert state["status"] == "stopped"
    assert process_identity(state["worker_pid"]) is None
    assert policy_status(path, policy, admission[0], vm.pid) is None


def test_worker_crash_quarantines_attached_vm(admission, vm, tmp_path):
    path = tmp_path / "policy.json"
    policy = NetworkPolicy(allowed_domains=["allowed.example"])
    prepared = PreparedPolicy(policy, admission[0], path)
    prepared.attach(vm.pid)
    state = policy_status(path, policy, admission[0], vm.pid)
    assert state is not None
    os.kill(state["worker_pid"], signal.SIGKILL)
    assert vm.wait(timeout=10) == -signal.SIGKILL
    prepared.process.wait(timeout=15)
    assert finished(path)["status"] == "failed"


def test_abort_before_attachment_closes_admission_and_releases_lock(admission, tmp_path):
    path = tmp_path / "policy.json"
    policy = NetworkPolicy(allowed_domains=["allowed.example"])
    prepared = PreparedPolicy(policy, admission[0], path)
    prepared.abort()
    prepared.process.wait(timeout=10)
    finished(path)
    with pytest.raises(RuntimeError, match="stopped-runtime cleanup"):
        PreparedPolicy(policy, admission[0], path)
    cleanup_stopped_policy(path, admission[0])
    replacement = PreparedPolicy(policy, admission[0], path)
    replacement.abort()
    replacement.process.wait(timeout=10)


def test_duplicate_owner_cannot_replace_active_policy(admission, vm, tmp_path):
    path = tmp_path / "policy.json"
    policy = NetworkPolicy(allowed_domains=["allowed.example"])
    prepared = PreparedPolicy(policy, admission[0], path)
    try:
        prepared.attach(vm.pid)
        with pytest.raises(RuntimeError, match="already has"):
            PreparedPolicy(policy, admission[0], path)
        with pytest.raises(RuntimeError, match="has not stopped"):
            cleanup_stopped_policy(path, admission[0])
        assert policy_status(path, policy, admission[0], vm.pid) is not None
        assert (
            policy_status(
                path, NetworkPolicy(allowed_domains=["other.example"]), admission[0], vm.pid
            )
            is None
        )
    finally:
        vm.terminate()
        vm.wait(timeout=5)
        prepared.process.wait(timeout=15)


def test_owner_sigkill_expires_admission_before_worker_releases_port(admission, vm, tmp_path):
    path = tmp_path / "policy.json"
    policy = NetworkPolicy(allowed_domains=["allowed.example"])
    prepared = PreparedPolicy(policy, admission[0], path)
    prepared.attach(vm.pid)
    state = policy_status(path, policy, admission[0], vm.pid)
    assert state is not None
    prepared.process.kill()
    prepared.process.wait(timeout=5)
    time.sleep(3.2)
    sets = json.loads(
        subprocess.check_output(
            ["nft", "-j", "list", "set", "inet", admission[0].table, "admitted_ports"]
        )
    )["nftables"]
    assert all(not item.get("set", {}).get("elem") for item in sets)
    assert process_identity(state["worker_pid"]) is not None
    assert policy_status(path, policy, admission[0], vm.pid) is None
    deadline = time.monotonic() + 6
    while process_identity(state["worker_pid"]) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert process_identity(state["worker_pid"]) is None
    with pytest.raises(RuntimeError, match="still running"):
        cleanup_stopped_policy(path, admission[0])
    vm.terminate()
    vm.wait(timeout=5)
    assert Path(state["worker_directory"]).exists()
    cleanup_stopped_policy(path, admission[0])
    assert not Path(state["worker_directory"]).exists()
    assert not path.exists()
    cleanup_stopped_policy(path, admission[0])  # Completed cleanup is idempotent.


def test_rule_drift_rejects_adoption_and_quarantines_vm(admission, vm, tmp_path):
    path = tmp_path / "policy.json"
    policy = NetworkPolicy(allowed_domains=["allowed.example"])
    prepared = PreparedPolicy(policy, admission[0], path)
    prepared.attach(vm.pid)
    # Counter/expiry changes during normal renewals are not configuration drift.
    time.sleep(1.2)
    assert policy_status(path, policy, admission[0], vm.pid) is not None
    subprocess.run(
        ["nft", "flush", "chain", "inet", admission[0].table, "forward"],
        check=True,
        capture_output=True,
    )
    assert policy_status(path, policy, admission[0], vm.pid) is None
    assert vm.wait(timeout=10) == -signal.SIGKILL
    prepared.process.wait(timeout=15)
    state = finished(path)
    assert state["status"] == "failed"


def test_deny_all_supervisor_has_no_worker_and_stops_with_vm(admission, vm, tmp_path):
    binding = replace(admission[0], proxy_uid=None, proxy_port=None, resolver_addresses=())
    policy = NetworkPolicy(allowed_domains=[])
    path = tmp_path / "deny.json"
    prepared = PreparedPolicy(policy, binding, path)
    assert prepared.public_ca == b""
    prepared.attach(vm.pid)
    state = policy_status(path, policy, binding, vm.pid)
    assert state is not None
    assert "worker_pid" not in state
    assert "worker_directory" not in state
    vm.terminate()
    vm.wait(timeout=5)
    prepared.process.wait(timeout=10)
    assert finished(path)["status"] == "stopped"
    cleanup_stopped_policy(path, binding)


def test_deny_all_drift_kills_vm(admission, vm, tmp_path):
    binding = replace(admission[0], proxy_uid=None, proxy_port=None, resolver_addresses=())
    path = tmp_path / "deny.json"
    prepared = PreparedPolicy(NetworkPolicy(allowed_domains=[]), binding, path)
    prepared.attach(vm.pid)
    binding.remove()
    assert vm.wait(timeout=10) == -signal.SIGKILL
    prepared.process.wait(timeout=10)
    assert finished(path)["status"] == "failed"


def test_native_shutdown_releases_only_after_processes_exit(admission, vm, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from network_policy import shutdown

    binding = admission[0]
    path = tmp_path / "native-stop.json"
    prepared = PreparedPolicy(NetworkPolicy(allowed_domains=["allowed.example"]), binding, path)
    prepared.attach(vm.pid)
    before = json.loads(path.read_text())
    monkeypatch.setattr(shutdown, "saved_placement", lambda info: (binding, path))
    shutdown.stop_policy(SimpleNamespace(vm_id="sbx-native-stop", pid=vm.pid), vm, timeout=5)
    assert vm.poll() is not None
    assert process_identity(before["supervisor_pid"]) is None
    assert process_identity(before["worker_pid"]) is None
    assert not path.exists()
    assert not Path(before["worker_directory"]).exists()
