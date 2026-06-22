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

"""Facade-level tests for the vsock control-channel seam.

These build a SmolVM via ``__new__`` (bypassing the SDK/create path) and drive
``_wait_for_ready`` directly, so they validate channel resolution + dispatch
without booting a VM.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from smolvm.comm.rust_http_vsock_channel import RustHttpVsockChannel
from smolvm.exceptions import OperationTimeoutError
from smolvm.facade import SmolVM
from smolvm.types import (
    CommandResult,
    GuestOS,
    NetworkConfig,
    VMConfig,
    VMInfo,
    VMState,
    VsockConfig,
)


def _vsock_vm(tmp_path: Path, *, comm_channel: str | None, request: str | None) -> SmolVM:
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.touch()
    rootfs.touch()
    config = VMConfig(
        vm_id="vm1",
        kernel_path=kernel,
        rootfs_path=rootfs,
        backend="qemu",
        guest_os=GuestOS.ALPINE,
        boot_args="console=ttyS0 reboot=k panic=1 init=/init",
        comm_channel=comm_channel,
        vsock=VsockConfig(guest_cid=5),
    )
    info = VMInfo(vm_id="vm1", status=VMState.RUNNING, config=config)

    vm = SmolVM.__new__(SmolVM)
    vm._comm_channel_request = request
    vm._vm_id = "vm1"
    vm._control_channel = None
    vm._control_ready = False
    vm._callbacks = MagicMock()
    vm._ssh = None
    vm._ssh_ready = False
    vm._info = info
    vm._sdk = MagicMock()
    vm._sdk.get.return_value = info
    return vm


def test_wait_for_ready_uses_rust_vsock_when_agent_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("smolvm.comm.select.host_supports_vsock", lambda: True)
    monkeypatch.setattr(
        RustHttpVsockChannel,
        "wait_ready",
        lambda self, timeout=60.0, interval=0.1: None,
    )

    vm = _vsock_vm(tmp_path, comm_channel="vsock", request=None)
    vm._wait_for_ready(timeout=5)

    assert vm._control_ready is True
    assert isinstance(vm._control_channel, RustHttpVsockChannel)
    assert vm._control_channel.guest_cid == 5
    assert vm._ssh is None


def test_public_vsock_ready_and_run_do_not_require_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("smolvm.comm.select.host_supports_vsock", lambda: True)
    monkeypatch.setattr(
        RustHttpVsockChannel,
        "wait_ready",
        lambda self, timeout=60.0, interval=0.1: None,
    )
    monkeypatch.setattr(
        RustHttpVsockChannel,
        "run",
        lambda self, command, timeout=30, shell="login": CommandResult(
            exit_code=0, stdout="ok", stderr=""
        ),
    )

    vm = _vsock_vm(tmp_path, comm_channel="vsock", request="vsock")

    vm.wait_for_ready(timeout=5)
    result = vm.run("printf ok")

    assert result.stdout == "ok"
    assert vm._control_ready is True
    assert vm._info.network is None


def test_wait_for_ssh_waits_for_ssh_not_vsock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SSH-named public API must not report readiness from the vsock agent."""
    vm = _vsock_vm(tmp_path, comm_channel="vsock", request="vsock")
    network = NetworkConfig(
        guest_ip="172.16.0.2",
        tap_device="tap0",
        guest_mac="02:00:00:00:00:01",
        ssh_host_port=2200,
    )
    vm._info = vm._info.model_copy(update={"network": network})
    vm._sdk.get.return_value = vm._info

    def _wait_for_ready(self, timeout: float) -> None:
        raise AssertionError("wait_for_ssh must not use the vsock control waiter")

    seen: dict[str, float | bool] = {}

    def _wait_for_ssh_over_network(self, timeout: float, *, as_control: bool = False) -> None:
        seen["timeout"] = timeout
        seen["as_control"] = as_control
        self._ssh_ready = True

    monkeypatch.setattr(SmolVM, "_wait_for_ready", _wait_for_ready)
    monkeypatch.setattr(SmolVM, "_wait_for_ssh_over_network", _wait_for_ssh_over_network)

    vm.wait_for_ssh(timeout=5)

    assert seen == {"timeout": 5, "as_control": False}
    assert vm._ssh_ready is True
    assert vm._control_ready is False


def test_explicit_vsock_does_not_fall_back_to_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("smolvm.comm.select.host_supports_vsock", lambda: True)

    def _fail(self, timeout=60.0, interval=0.1):
        raise OperationTimeoutError("vsock", timeout)

    monkeypatch.setattr(RustHttpVsockChannel, "wait_ready", _fail)

    # request="vsock" -> explicit
    vm = _vsock_vm(tmp_path, comm_channel="vsock", request="vsock")
    with pytest.raises(OperationTimeoutError):
        vm._wait_for_ready(timeout=1)
    assert vm._control_ready is False


def test_auto_vsock_requires_guest_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent-less image must fail vsock readiness instead of hiding it behind SSH."""
    monkeypatch.setattr("smolvm.comm.select.host_supports_vsock", lambda: True)

    seen = {"probe_timeouts": []}

    def _fail(self, timeout=60.0, interval=0.1):
        seen["probe_timeouts"].append(timeout)
        raise OperationTimeoutError("vsock", timeout)

    monkeypatch.setattr(RustHttpVsockChannel, "wait_ready", _fail)

    ssh_called = {}

    def _ssh_ok(self, timeout, *, as_control=False):
        ssh_called["timeout"] = timeout
        ssh_called["as_control"] = as_control
        self._control_ready = True

    monkeypatch.setattr(SmolVM, "_wait_for_ssh_over_network", _ssh_ok)

    # auto channel (comm_channel=None, request=None) -> vsock, agent required
    vm = _vsock_vm(tmp_path, comm_channel=None, request=None)
    with pytest.raises(OperationTimeoutError) as exc:
        vm._wait_for_ready(timeout=30)

    assert (
        "Sandbox 'vm1' did not become ready; run 'smolvm sandbox delete vm1' "
        "and then 'smolvm sandbox create --name vm1 --comm-channel ssh'"
    ) in str(exc.value)
    assert seen["probe_timeouts"] == [30]
    assert ssh_called == {}
    assert vm._control_ready is False


def test_resolve_channel_reads_config_and_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("smolvm.comm.select.host_supports_vsock", lambda: True)
    vm = _vsock_vm(tmp_path, comm_channel=None, request=None)
    # auto on linux+qemu -> vsock, agent required
    res = vm._resolve_channel()
    assert res.kind == "vsock"
