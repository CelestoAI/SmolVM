"""Unit tests for the QEMU runtime adapter."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.runtime.base import RuntimeContext
from smolvm.runtime.qemu import QemuRuntimeAdapter
from smolvm.types import NetworkConfig, VMConfig, VMInfo, VMState


def _make_context() -> RuntimeContext:
    """Build a minimal runtime context with mockable process hooks."""
    return RuntimeContext(
        data_dir=Path("/tmp/data"),
        socket_dir=Path("/tmp"),
        firmware_dir=Path("/tmp/data/firmware"),
        log_files={},
        process_handles={},
        resolve_boot_args=lambda vm_info: vm_info.config.boot_args,
        start_firecracker=MagicMock(),
        start_qemu=MagicMock(),
        unlink_socket=MagicMock(),
        kill_process=MagicMock(),
        wait_for_process=MagicMock(),
        is_process_running=MagicMock(),
        find_qemu_binary=MagicMock(),
    )


def _make_vm_info(tmp_path: Path, *, pid: int = 12345) -> VMInfo:
    """Create a minimal QEMU-backed VMInfo for adapter tests."""
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    socket_path = tmp_path / "qmp.sock"
    kernel.touch()
    rootfs.touch()
    socket_path.touch()

    return VMInfo(
        vm_id="vm-qemu-stop",
        status=VMState.RUNNING,
        config=VMConfig(
            vm_id="vm-qemu-stop",
            kernel_path=kernel,
            rootfs_path=rootfs,
            backend="qemu",
            boot_args="console=ttyAMA0 reboot=k panic=1 init=/init",
        ),
        network=NetworkConfig(
            guest_ip="10.0.2.15",
            gateway_ip="10.0.2.2",
            netmask="255.255.255.0",
            tap_device="usernet",
            guest_mac="aa:fc:00:00:00:01",
            ssh_host_port=2200,
        ),
        pid=pid,
        control_socket_path=socket_path,
    )


def test_stop_waits_for_hard_kill_before_releasing_socket(tmp_path: Path) -> None:
    """Forced QEMU termination should wait for exit before cleanup proceeds."""
    context = _make_context()
    context.is_process_running.side_effect = [True, True, False]
    adapter = QemuRuntimeAdapter(context)
    vm_info = _make_vm_info(tmp_path)

    with patch("os.kill") as mock_os_kill:
        adapter.stop(vm_info, timeout=10.0)

    mock_os_kill.assert_called_once()
    context.kill_process.assert_called_once_with(vm_info.pid)
    assert context.wait_for_process.call_args_list == [
        call(vm_info.pid, 10.0),
        call(vm_info.pid, 5.0),
    ]
    context.unlink_socket.assert_called_once_with(vm_info.control_socket_path)


def test_stop_raises_when_qemu_survives_hard_kill(tmp_path: Path) -> None:
    """Cleanup should fail loudly if the QEMU process still has not exited."""
    context = _make_context()
    context.is_process_running.side_effect = [True, True, True]
    adapter = QemuRuntimeAdapter(context)
    vm_info = _make_vm_info(tmp_path)

    with patch("os.kill") as mock_os_kill, pytest.raises(SmolVMError, match="did not exit"):
        adapter.stop(vm_info, timeout=10.0)

    mock_os_kill.assert_called_once()
    context.kill_process.assert_called_once_with(vm_info.pid)
    assert context.wait_for_process.call_args_list == [
        call(vm_info.pid, 10.0),
        call(vm_info.pid, 5.0),
    ]
    context.unlink_socket.assert_not_called()


def test_qcow2_backing_inspection_force_shares_running_qemu_disk(tmp_path: Path) -> None:
    """Inspecting a paused-but-open QEMU disk must bypass qemu-img's image lock."""
    disk = tmp_path / "vm.qcow2"
    disk.touch()
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='{"full-backing-filename": "/tmp/base.qcow2"}',
        stderr="",
    )

    with (
        patch("smolvm.runtime.qemu.which", return_value=Path("/usr/bin/qemu-img")),
        patch("smolvm.runtime.qemu.subprocess.run", return_value=result) as mock_run,
    ):
        backing = QemuRuntimeAdapter._qcow2_backing_file_required(disk)

    assert backing == Path("/tmp/base.qcow2")
    mock_run.assert_called_once_with(
        ["/usr/bin/qemu-img", "info", "-U", "--output=json", str(disk)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.asyncio
async def test_strict_cancelled_start_waits_for_boot_and_shutdown(tmp_path):
    import asyncio
    import threading

    from smolvm.network_policy import NetworkPolicy
    from smolvm.runtime.base import RuntimeLaunch

    context = _make_context()
    adapter = QemuRuntimeAdapter(context)
    vm = _make_vm_info(tmp_path)
    vm = vm.model_copy(
        update={
            "config": vm.config.model_copy(
                update={"network_policy": NetworkPolicy(allowed_domains=[])}
            )
        }
    )
    booting, finish_boot, stopping, finish_stop = (threading.Event() for _ in range(4))
    process = MagicMock()
    context.process_handles[12345] = process

    def start(*args, **kwargs):
        booting.set()
        assert finish_boot.wait(5)
        return RuntimeLaunch(
            pid=12345, control_socket_path=tmp_path / "qmp", status=VMState.RUNNING
        )

    def wait(**kwargs):
        stopping.set()
        assert finish_stop.wait(5)

    process.wait.side_effect = wait
    adapter.start = start
    task = asyncio.create_task(adapter.async_start(vm, log_path=tmp_path / "log", boot_timeout=1))
    try:
        assert await asyncio.to_thread(booting.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish_boot.set()
        assert await asyncio.to_thread(stopping.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish_stop.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        process.kill.assert_called_once()
        process.wait.assert_called_once_with(timeout=10)
        context.kill_process.assert_not_called()
    finally:
        finish_boot.set()
        finish_stop.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_strict_cancelled_start_preserves_shutdown_failure(tmp_path):
    import asyncio
    import threading

    from smolvm.network_policy import NetworkPolicy

    context = _make_context()
    adapter = QemuRuntimeAdapter(context)
    vm = _make_vm_info(tmp_path)
    vm = vm.model_copy(
        update={
            "config": vm.config.model_copy(
                update={"network_policy": NetworkPolicy(allowed_domains=[])}
            )
        }
    )
    booting, finish = threading.Event(), threading.Event()

    def start(*args, **kwargs):
        booting.set()
        assert finish.wait(5)
        raise RuntimeError("shutdown unverified")

    adapter.start = start
    task = asyncio.create_task(adapter.async_start(vm, log_path=tmp_path / "log", boot_timeout=1))
    try:
        assert await asyncio.to_thread(booting.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        finish.set()
        with pytest.raises(RuntimeError, match="shutdown unverified"):
            await task
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("failure", [None, "trust", "policy"])
def test_strict_start_installs_trust_before_return(tmp_path, failure):
    from smolvm.network_policy import NetworkPolicy

    context = _make_context()
    adapter = QemuRuntimeAdapter(context)
    vm = _make_vm_info(tmp_path)
    vm = vm.model_copy(
        update={
            "config": vm.config.model_copy(
                update={"network_policy": NetworkPolicy(allowed_domains=["example.com"])}
            )
        }
    )
    events = []
    process = MagicMock(pid=12345)
    process.kill.side_effect = lambda: events.append("kill")
    process.wait.side_effect = lambda **kwargs: events.append("wait")
    context.start_qemu.return_value = process
    client = MagicMock()
    client.__enter__.return_value.cont.side_effect = lambda: events.append("cont")

    def trust(*args, **kwargs):
        events.append("trust")
        if failure == "trust":
            raise RuntimeError("trust failed")

    def verify(*args):
        events.append("verify")
        if failure == "policy":
            raise RuntimeError("policy stopped")

    with (
        patch.object(
            adapter, "_resolve_platform_spec", return_value=MagicMock(requires_swtpm=False)
        ),
        patch.object(adapter, "_firmware_vars_path", return_value=None),
        patch.object(adapter, "_wait_for_runtime"),
        patch.object(adapter, "_client", return_value=client),
        patch("smolvm.network_policy.guest.configure_started_guest", side_effect=trust) as setup,
        patch("smolvm.network_policy.placement.active_policy", side_effect=verify),
    ):
        if failure == "policy":
            with pytest.raises(RuntimeError, match="policy stopped"):
                adapter.start(vm, log_path=tmp_path / "log", boot_timeout=7)
            assert events == ["verify", "kill", "wait"]
            setup.assert_not_called()
            return
        if failure == "trust":
            with pytest.raises(RuntimeError, match="trust failed"):
                adapter.start(vm, log_path=tmp_path / "log", boot_timeout=7)
            assert events == ["verify", "cont", "trust", "kill", "wait"]
        else:
            result = adapter.start(vm, log_path=tmp_path / "log", boot_timeout=7)
            assert result.pid == 12345
            assert events == ["verify", "cont", "trust"]
        setup.assert_called_once_with(vm, 12345, timeout=7)


@pytest.mark.parametrize("healthy", [False, True])
def test_strict_resume_checks_policy_before_cont(tmp_path, healthy):
    from smolvm.network_policy import NetworkPolicy

    adapter = QemuRuntimeAdapter(_make_context())
    vm = _make_vm_info(tmp_path)
    vm = vm.model_copy(
        update={
            "config": vm.config.model_copy(
                update={"network_policy": NetworkPolicy(allowed_domains=[])}
            )
        }
    )
    events = []
    client = MagicMock()
    client.__enter__.return_value.cont.side_effect = lambda: events.append("cont")

    def check(*args):
        events.append("verify")
        if not healthy:
            raise RuntimeError("policy stopped")

    with (
        patch.object(adapter, "_client", return_value=client) as connect,
        patch("smolvm.network_policy.placement.active_policy", side_effect=check),
    ):
        if healthy:
            adapter.resume(vm)
            assert events == ["verify", "cont"]
        else:
            with pytest.raises(RuntimeError, match="policy stopped"):
                adapter.resume(vm)
            connect.assert_not_called()
            assert events == ["verify"]
