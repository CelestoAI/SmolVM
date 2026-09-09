"""Real QEMU application contract; opt in with a disposable baked test image.

Set SMOLVM_QEMU_POLICY_CONFIG to a VMConfig JSON using qemu-policy-init.sh
and qemu-policy-app.py from assets/. Works on Linux/KVM and macOS/HVF.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

import pytest
from test_network_policy import policy_lab  # noqa: F401

from smolvm import SmolVM
from smolvm.images import BootImage, DirectKernelBoot
from smolvm.storage import MemoryStateManager
from smolvm.types import InternetSettings, VMConfig

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("SMOLVM_QEMU_POLICY_CONFIG"),
        reason="Requires disposable QEMU test image",
    ),
]


def request(url, data=None):
    with build_opener(ProxyHandler({})).open(Request(url, data=data), timeout=5) as reply:
        return reply.read()


def ready(url):
    deadline = time.monotonic() + 30
    while True:
        try:
            assert request(url) == b"ready\n"
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def probe(url, host, port, transport="tcp"):
    query = urlencode({"host": host, "port": port, "transport": transport})
    return json.loads(request(f"{url}probe?{query}"))["reached"]


def websocket_echo(port):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        stream = connection.makefile("rb")
        connection.sendall(
            b"GET /ws HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
        )
        assert b"101" in stream.readline()
        while stream.readline() != b"\r\n":
            pass
        payload, mask = b"hello", b"abcd"
        connection.sendall(
            bytes([0x81, 0x80 | len(payload)])
            + mask
            + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        )
        assert stream.read(2) == bytes([0x81, len(payload)])
        assert stream.read(len(payload)) == payload


@pytest.fixture
def template():
    return json.loads(Path(os.environ["SMOLVM_QEMU_POLICY_CONFIG"]).read_text())


@pytest.mark.parametrize("mode", ["open", "off", "restricted"])
@pytest.mark.parametrize("constructor", ["config", "from_image"])
def test_application_without_control_agent(template, tmp_path, monkeypatch, mode, constructor):
    tap = template.get("qemu_network", "slirp") == "tap"
    if mode == "restricted" and not tap:
        pytest.skip("CIDRs require Linux TAP")
    # Linux TAP callers can set the controlled lab's allowed address here.
    allowed = os.environ.get("SMOLVM_QEMU_POLICY_ALLOWED", "1.1.1.1")
    policy = InternetSettings(mode=mode, allowed_cidrs=[allowed] if mode == "restricted" else [])
    values = dict(template, vm_id=f"qpolicy-{uuid4().hex[:12]}", internet_settings=policy)
    launch_port = free_port() if not tap else None
    if launch_port:
        values["port_forwards"] = [{"host_port": launch_port, "guest_port": 18080}]
    config = VMConfig.model_validate(values)
    inventory = MemoryStateManager(tmp_path)
    if constructor == "config":
        vm = SmolVM(config=config, data_dir=tmp_path, state_manager=inventory)
    else:
        image = BootImage(
            name="qemu-policy-app",
            rootfs_path=config.rootfs_path,
            rootfs_format=config.rootfs_format,
            kernel_path=config.kernel_path,
            boot=DirectKernelBoot(init="/policy-init"),
        )
        vm = SmolVM.from_image(
            image,
            backend="qemu",
            network=config.qemu_network,
            guest_os=config.guest_os,
            internet_settings=policy,
            comm_channel="ssh",
            port_forwards=config.port_forwards,
            data_dir=tmp_path,
            state_manager=inventory,
            memory_mb=config.memory,
            vcpus=config.vcpu_count,
        )
    snapshot_id = None
    sdk = vm._sdk
    # Fail loudly if ordinary application access takes a control-channel detour.
    monkeypatch.setattr(
        SmolVM,
        "_ensure_control_for_operation",
        lambda *a, **kw: pytest.fail("Application used a command connection"),
    )
    monkeypatch.setattr(
        SmolVM,
        "_start_local_tunnel",
        lambda *a, **kw: pytest.fail("Application used an SSH tunnel"),
    )
    try:
        asyncio.run(vm.async_start()) if constructor == "config" else vm.start()
        url = (
            f"http://{vm.info.network.guest_ip}:18080/"
            if tap
            else f"http://127.0.0.1:{launch_port}/"
        )
        ready(url)
        port = vm.expose_local(18080)
        local_url = f"http://127.0.0.1:{port}/"
        ready(local_url)
        payload = bytes(range(256)) * 4096
        assert request(local_url, payload) == payload
        websocket_echo(port)
        if mode == "open" and not tap:
            assert probe(local_url, "1.1.1.1", 443)
            assert probe(local_url, "10.0.2.3", 53, "dns")
        if mode == "off":
            assert not probe(local_url, "1.1.1.1", 443)
            assert not probe(local_url, "8.8.8.8", 53, "dns")
            assert not probe(local_url, "2606:4700:4700::1111", 443)
            if not tap:
                assert not probe(local_url, "10.0.2.3", 53, "dns")
                assert not probe(local_url, "10.0.2.2", 22)
        vm.pause()
        asyncio.run(vm.async_start()) if constructor == "config" else vm.resume()
        ready(url)
        port = vm.expose_local(18080)  # Pause also closes dynamic exposures, as before.
        local_url = f"http://127.0.0.1:{port}/"
        ready(local_url)
        vm._sdk.ensure_network_connectivity(vm.info)
        ready(local_url)
        vm.stop().start()
        ready(url)
        port = vm.expose_local(18080)  # Dynamic exposures have the existing stop lifetime.
        ready(f"http://127.0.0.1:{port}/")
        snapshot_id = vm.snapshot(snapshot_type="disk", flush_policy="skip").snapshot_id
        vm.stop()
        vm.delete()
        vm = None
        vm = SmolVM.from_snapshot(
            snapshot_id, backend="qemu", data_dir=tmp_path, state_manager=inventory, resume_vm=True
        )
        assert vm.info.config.internet_settings == policy
        ready(url)
        ready(f"http://127.0.0.1:{vm.expose_local(18080)}/")
        if mode == "off":
            assert not probe(url, "1.1.1.1", 443)
    except BaseException:
        if vm is not None:
            log = tmp_path / f"{vm.vm_id}.log"
            if log.exists():
                print(log.read_text(errors="replace")[-10000:])
        raise
    finally:
        if vm is not None:
            try:
                vm.stop()
            finally:
                vm.delete()
        if snapshot_id:
            sdk.delete_snapshot(snapshot_id)


@pytest.mark.parametrize("writable", [False, True])
@pytest.mark.parametrize("mode", ["off", "restricted"])
def test_policy_keeps_sdk_files_and_shared_folders(template, tmp_path, writable, mode):
    if mode == "restricted" and template.get("qemu_network", "slirp") != "tap":
        pytest.skip("CIDRs require Linux TAP")
    # Same cached image, but its ordinary /init supplies the existing SSH setup
    # needed by shared folders. Application access above does not need it.
    template["boot_args"] = template["boot_args"].replace("init=/policy-init", "init=/init")
    key = tmp_path / "ssh-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    config = VMConfig.model_validate(
        dict(
            template,
            vm_id=f"qfolder-{uuid4().hex[:12]}",
            ssh_capable=True,
            comm_channel="ssh",
            ssh_public_key=key.with_suffix(".pub").read_text().strip(),
        )
    )
    folder = tmp_path / "shared"
    folder.mkdir()
    (folder / "hello.txt").write_text("shared hello")
    with SmolVM(
        config=config,
        data_dir=tmp_path / "data",
        internet_settings={
            "mode": mode,
            "allowed_cidrs": ["198.19.0.1/32"] if mode == "restricted" else [],
        },
        mounts=[f"{folder}:/work"],
        writable_mounts=writable,
        ssh_key_path=str(key),
    ) as vm:
        assert vm.run("cat /work/hello.txt").stdout.strip() == "shared hello"
        assert vm.run("printf changed > /work/hello.txt").exit_code == 0
        assert (folder / "hello.txt").read_text() == ("changed" if writable else "shared hello")
        source, downloaded = tmp_path / "upload.txt", tmp_path / "download.txt"
        source.write_text("SDK file transfer")
        vm.upload_file(source, "/tmp/upload.txt")
        vm.download_file("/tmp/upload.txt", downloaded)
        assert downloaded.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("mode", ["off", "restricted"])
def test_tap_destinations_and_real_install_failure(
    template,
    tmp_path,
    monkeypatch,
    mode,
    policy_lab,  # noqa: F811
):
    if template.get("qemu_network") != "tap":
        pytest.skip("Requires Linux TAP configuration")
    # Reuse the existing disposable lab instead of making a second firewall lab.
    from smolvm.exceptions import SmolVMError, VMNotFoundError
    from smolvm.host.network import NetworkManager

    allowed, denied, *_ = policy_lab
    inventory = MemoryStateManager(tmp_path / "inventory")
    values = dict(template, vm_id=f"qtap-{uuid4().hex[:12]}")
    vm = None
    snapshot_id = None
    sdk = None
    run_nft = NetworkManager._run_nft_script

    def reject(self, script):
        if "smolvm_policy_" in script:
            script += "\ninvalid nft syntax\n"
        return run_nft(self, script)

    def check(vm):
        url = f"http://{vm.info.network.guest_ip}:18080/"
        ready(url)
        assert probe(url, allowed, 18080) is (mode == "restricted")
        assert probe(url, allowed, 53, "udp") is (mode == "restricted")
        assert not probe(url, denied, 18080)
        assert not probe(url, denied, 53, "udp")
        ready(f"http://127.0.0.1:{vm.expose_local(18080)}/")

    try:
        # Positive controls traverse a real VM, not just the host's network.
        with SmolVM(config=VMConfig.model_validate(values), data_dir=tmp_path / "open") as control:
            url = f"http://{control.info.network.guest_ip}:18080/"
            ready(url)
            assert probe(url, allowed, 18080)
            assert probe(url, denied, 18080)
            assert probe(url, denied, 53, "udp")
        values["internet_settings"] = {
            "mode": mode,
            "allowed_cidrs": [allowed] if mode == "restricted" else [],
        }
        config = VMConfig.model_validate(values)
        with monkeypatch.context() as fault:
            fault.setattr(NetworkManager, "_run_nft_script", reject)
            with pytest.raises(SmolVMError):
                SmolVM(config=config, data_dir=tmp_path, state_manager=inventory)
        vm = SmolVM(config=config, data_dir=tmp_path, state_manager=inventory)
        sdk = vm._sdk
        with monkeypatch.context() as fault:
            fault.setattr(NetworkManager, "_run_nft_script", reject)
            with pytest.raises(SmolVMError):
                vm.start()
        assert vm.info.pid is None
        vm.start()
        check(vm)
        with monkeypatch.context() as fault:
            fault.setattr(NetworkManager, "_run_nft_script", reject)
            with pytest.raises(SmolVMError):
                sdk.ensure_network_connectivity(vm.info)
            # Read existing application/policy state without repairing it again.
            url = f"http://{vm.info.network.guest_ip}:18080/"
            ready(url)
            assert not probe(url, denied, 18080)
        vm.pause()
        with monkeypatch.context() as fault:
            fault.setattr(NetworkManager, "_run_nft_script", reject)
            with pytest.raises(SmolVMError):
                vm.resume()
        assert vm.info.status.value == "paused"
        vm.resume()
        check(vm)
        snapshot_id = vm.snapshot(snapshot_type="disk", flush_policy="skip").snapshot_id
        vm.stop().delete()
        vm = None
        with monkeypatch.context() as fault:
            fault.setattr(NetworkManager, "_run_nft_script", reject)
            with pytest.raises(SmolVMError):
                SmolVM.from_snapshot(
                    snapshot_id,
                    backend="qemu",
                    data_dir=tmp_path,
                    state_manager=inventory,
                    resume_vm=True,
                )
        with pytest.raises(VMNotFoundError):
            inventory.get_vm(config.vm_id)
        vm = SmolVM.from_snapshot(
            snapshot_id, backend="qemu", data_dir=tmp_path, state_manager=inventory, resume_vm=True
        )
        check(vm)
    finally:
        if vm is not None:
            vm.stop().delete()
        if snapshot_id:
            sdk.delete_snapshot(snapshot_id)
